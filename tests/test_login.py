"""`qswap login`: a new sign-in in a private profile, saved over an account's login.

No test runs the real `claude`: `login.renew` takes a runner, and the fake
below writes the private profile the way Claude Code does (`.claude.json`
with `oauthAccount`, and the credential in the profile's Keychain item on
macOS, in its `.credentials.json` elsewhere). Every test runs on both
storage paths (`platform` pins one), so the Linux and Windows jobs cover the
file path and the macOS one the Keychain item, against the suite's in-memory
Keychain. The real runner is exercised against a stand-in program.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

from ai_quota_swap import login, macos_keychain, menubar, menubar_engines, menubar_notify, oauth
from ai_quota_swap.exceptions import (
    AccountNotFoundError,
    LoginError,
    SwitchError,
    ValidationError,
)
from ai_quota_swap.locking import FileLock
from ai_quota_swap.login import LoginRun, Renewal, Target
from ai_quota_swap.login import run_claude_login as REAL_RUN  # before conftest's guard
from ai_quota_swap.menubar import AccountState, Action, MenuBarSettings, MenuBarState, MenuInput, ProviderState
from ai_quota_swap.models import Platform
from ai_quota_swap.session import keychain_service_name, session_dir_for, stale_marker_for
from ai_quota_swap.switcher import ClaudeAccountSwitcher
from ai_quota_swap.usage_store import FetchRecord

DAY = 86400.0


def _creds(tag: str, *, login_left: float = 20 * DAY, mcp: dict | None = None) -> str:
    now = time.time()
    data: dict = {"claudeAiOauth": {
        "accessToken": f"sk-ant-oat01-{tag}", "refreshToken": f"sk-ant-ort01-{tag}",
        "expiresAt": int((now + 8 * 3600) * 1000),
        "refreshTokenExpiresAt": int((now + login_left) * 1000),
        "scopes": ["user:inference", "user:profile"],
    }}
    if mcp is not None:
        data["mcpOAuth"] = mcp
    return json.dumps(data)


def _fp(creds: str | None) -> str | None:
    return oauth.credential_fingerprint(creds or "")


@pytest.fixture(params=["file", "keychain"])
def platform(request, monkeypatch):
    """The two places Claude Code keeps a profile's login: its
    `.credentials.json` (Linux, Windows) or a Keychain item named after the
    profile (macOS). Pinned before any switcher is built."""
    pin = Platform.LINUX if request.param == "file" else Platform.MACOS
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: pin))
    return pin


class FakeClaude:
    """`claude auth login` as a runner: writes the private profile the way
    Claude Code does, then answers with ``returncode``."""

    def __init__(self, email: str = "", *, org: str = "", uuid: str = "uuid-2", creds: str | None = None,
                 returncode: int | None = 0, timed_out: bool = False, raises: BaseException | None = None,
                 write: bool = True, write_creds: bool = True, org_name: str = "", detail: str = ""):
        self.email, self.org, self.uuid, self.org_name = email, org, uuid, org_name
        self.creds = creds or _creds("new", login_left=30 * DAY)
        self.returncode, self.timed_out, self.raises, self.detail = returncode, timed_out, raises, detail
        self.write, self.write_creds = write, write_creds
        self.calls: list[dict] = []
        self.profile: Path | None = None

    def __call__(self, profile: Path, email: str, *, sso=False, interactive=True, timeout=None) -> LoginRun:
        self.profile = profile
        self.calls.append({"profile": profile, "email": email, "sso": sso,
                           "interactive": interactive, "timeout": timeout})
        assert profile.is_dir()
        if os.name != "nt":
            assert stat.S_IMODE(profile.stat().st_mode) == 0o700
        if self.write:
            (profile / ".claude.json").write_text(json.dumps({
                "oauthAccount": {
                    "emailAddress": self.email or email, "organizationUuid": self.org or None,
                    "accountUuid": self.uuid, "displayName": "New Name",
                    "organizationName": self.org_name, "billingType": "stripe_subscription",
                },
                "userID": "throwaway-profile", "numStartups": 0,
            }), encoding="utf-8")
            if self.write_creds:
                if Platform.detect() == Platform.MACOS:
                    macos_keychain.set_password(
                        keychain_service_name(str(profile)), macos_keychain.keychain_account_name(), self.creds,
                    )
                else:
                    (profile / ".credentials.json").write_text(self.creds, encoding="utf-8")
        if self.raises is not None:
            raise self.raises
        return LoginRun(self.returncode, self.detail, timed_out=self.timed_out)


def _seed(s: ClaudeAccountSwitcher, num: int, email: str, creds: str, *, org: str = "", **fields) -> None:
    s._write_account_credentials(str(num), email, creds)
    s._write_account_config(str(num), email, json.dumps({
        "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}",
                         "organizationUuid": org or None, "displayName": "Old Name"},
        "projects": {"/work/app": {"allowedTools": ["Bash"]}},
        "userID": f"user-{num}",
    }))
    data = s._get_sequence_data()
    data["accounts"][str(num)] = {
        "email": email, "uuid": f"uuid-{num}", "organizationUuid": org,
        "organizationName": "Northwind" if org else "", "added": "2026-01-01T00:00:00Z", **fields,
    }
    if num not in data["sequence"]:
        data["sequence"].append(num)
        data["sequence"].sort()
    s._write_json(s.sequence_file, data)


def _go_live(s: ClaudeAccountSwitcher, num: int, email: str, creds: str, *, org: str = "") -> None:
    s._write_credentials(creds)
    s._get_claude_config_path().write_text(json.dumps({
        "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}",
                         "organizationUuid": org or None, "displayName": "Old Name"},
        "projects": {"/live/repo": {"allowedTools": []}},
        "numStartups": 42,
    }), encoding="utf-8")
    data = s._get_sequence_data()
    data["activeAccountNumber"] = num
    s._write_json(s.sequence_file, data)


@pytest.fixture
def fleet(temp_home, platform):
    """#1 personal, the live login; #2 work, saved, its login due in 2 days,
    with every setting a user can put on an account."""
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._init_sequence_file()
    _seed(s, 1, "maya@example.com", _creds("one"), alias="personal")
    _seed(s, 2, "maya@northwind.example.com", _creds("two-old", login_left=2 * DAY),
          alias="work", disabled=True, threshold=80.0, priority=7)
    _go_live(s, 1, "maya@example.com", _creds("one"))
    return s


def _target(s, identifier="2") -> Target:
    return login.resolve(s, identifier)


def _profiles_left(s) -> list[str]:
    root = login.sign_in_root(s)
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


def _keychain_items_for(store, profile: Path) -> list:
    if store is None:
        return []
    return [k for k in store.data if k[0] == keychain_service_name(str(profile))]


# -- an account that is not the live login -------------------------------------------


def test_renews_a_saved_login_and_keeps_everything_set_on_it(fleet, block_real_keychain):
    s = fleet
    fake = FakeClaude("maya@northwind.example.com")
    renewal = login.renew(s, _target(s), runner=fake, interactive=False)

    assert fake.calls[0]["email"] == "maya@northwind.example.com"
    assert fake.calls[0]["interactive"] is False and fake.calls[0]["timeout"] == login.DEFAULT_TIMEOUT_S
    assert _fp(s.read_account_credentials("2", "maya@northwind.example.com")) == _fp(fake.creds)
    record = s._get_sequence_data()["accounts"]["2"]
    assert {k: record[k] for k in ("alias", "disabled", "threshold", "priority")} == {
        "alias": "work", "disabled": True, "threshold": 80.0, "priority": 7,
    }
    # The saved config keeps what a switch restores from it; oauthAccount is updated.
    saved = json.loads(s.read_account_config("2", "maya@northwind.example.com"))
    assert saved["projects"] == {"/work/app": {"allowedTools": ["Bash"]}}
    assert saved["userID"] == "user-2"
    assert saved["oauthAccount"]["displayName"] == "New Name"
    assert saved["oauthAccount"]["billingType"] == "stripe_subscription"
    # The live login is #1's, untouched.
    assert _fp(s._read_credentials()) == _fp(_creds("one"))
    assert renewal == Renewal("2", "#2 work", oauth.login_expires_at_epoch(fake.creds), live=False, key=None)
    assert login.renewed_text(renewal).startswith("Renewed the login of #2 work; the next sign-in is due ")
    assert login.renewed_text(renewal).endswith(" · in 29d 23h")
    # Nothing of the private profile is left, Keychain item included.
    assert _profiles_left(s) == [".lock"]
    assert _keychain_items_for(block_real_keychain, fake.profile) == []
    assert not fake.profile.exists()


def test_the_private_profile_is_its_own_place(fleet):
    s = fleet
    fake = FakeClaude("maya@northwind.example.com")
    login.renew(s, _target(s), runner=fake, interactive=False)
    assert fake.profile.parent == s.backup_dir / "sign-in"
    assert (s.backup_dir / "sessions") not in fake.profile.parents


def test_a_renewal_clears_the_dead_login_state(fleet):
    s = fleet
    ident = {"2": ("maya@northwind.example.com", "")}
    for _ in range(2):
        s._usage_store.record({"2": FetchRecord(error="invalid_grant")}, ident)
    assert s._usage_store.entries(ident)["2"].token_dead()
    login.renew(s, _target(s), runner=FakeClaude("maya@northwind.example.com"), interactive=False)
    assert not s._usage_store.entries(ident)["2"].token_dead()


def test_its_qswap_run_profile_drops_the_old_login(fleet):
    """What `add` does to the account's session profile after a re-save:
    with nothing running there its login is dropped, so the next `qswap run`
    copies the new one in."""
    s = fleet
    profile = session_dir_for(s.backup_dir, "2", "maya@northwind.example.com")
    profile.mkdir(parents=True)
    (profile / ".credentials.json").write_text(_creds("two-old"), encoding="utf-8")
    (profile / "history.jsonl").write_text("{}\n", encoding="utf-8")
    login.renew(s, _target(s), runner=FakeClaude("maya@northwind.example.com"), interactive=False)
    assert not (profile / ".credentials.json").exists()
    assert (profile / "history.jsonl").exists()


def test_a_running_qswap_run_session_is_marked_stale(fleet, monkeypatch):
    s = fleet
    profile = session_dir_for(s.backup_dir, "2", "maya@northwind.example.com")
    profile.mkdir(parents=True)
    (profile / ".credentials.json").write_text(_creds("two-old"), encoding="utf-8")
    monkeypatch.setattr(s, "_live_session_pids", lambda num, email: [4242] if num == "2" else [])
    login.renew(s, _target(s), runner=FakeClaude("maya@northwind.example.com"), interactive=False)
    assert stale_marker_for(profile).exists()
    assert (profile / ".credentials.json").exists()  # the running session keeps its own


# -- the active account -------------------------------------------------------------------


def test_renewing_the_active_account_replaces_the_live_login(fleet):
    s = fleet
    mcp = {"linear": {"accessToken": "mcp-live"}}
    s._write_credentials(_creds("one", mcp=mcp))
    fake = FakeClaude("maya@example.com", uuid="uuid-1")
    renewal = login.renew(s, _target(s, "1"), runner=fake, interactive=False)

    assert renewal.live is True and renewal.key is None
    live = s._read_credentials()
    assert _fp(live) == _fp(fake.creds)
    assert json.loads(live)["mcpOAuth"] == mcp  # the live MCP sign-ins composed in, as a switch does
    assert _fp(s.read_account_credentials("1", "maya@example.com")) == _fp(fake.creds)
    config = json.loads(s._get_claude_config_path().read_text(encoding="utf-8"))
    assert config["projects"] == {"/live/repo": {"allowedTools": []}} and config["numStartups"] == 42
    assert config["oauthAccount"]["emailAddress"] == "maya@example.com"
    assert config["oauthAccount"]["displayName"] == "New Name"
    assert s.login_account_number() == "1"
    assert s._get_sequence_data()["activeAccountNumber"] == 1


def test_the_resync_does_not_undo_a_renewal(fleet, monkeypatch):
    """The usage pass copies a rotated live credential over the active
    account's saved one. A pass that read the old live login just before
    the renewal must not copy it back afterwards."""
    s = fleet
    old = _creds("one")
    monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda token: {
        "uuid": "uuid-1", "email": "maya@example.com", "organizationUuid": "",
    })
    fake = FakeClaude("maya@example.com", uuid="uuid-1")
    login.renew(s, _target(s, "1"), runner=fake, interactive=False)
    s._resync_rotated_backup("1", "maya@example.com", "", old)
    assert _fp(s.read_account_credentials("1", "maya@example.com")) == _fp(fake.creds)
    assert _fp(s._read_credentials()) == _fp(fake.creds)


def test_CONTROL_a_renewal_of_the_saved_copy_alone_is_undone_by_the_resync(fleet, monkeypatch):
    """Why the live login has to be replaced too: with only the saved copy
    renewed, the same resync call writes the old live login back over it."""
    s = fleet
    old = _creds("one")
    monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda token: {
        "uuid": "uuid-1", "email": "maya@example.com", "organizationUuid": "",
    })
    s._write_account_credentials("1", "maya@example.com", _creds("renewed"))
    s._resync_rotated_backup("1", "maya@example.com", "", old)
    assert _fp(s.read_account_credentials("1", "maya@example.com")) == _fp(old)


def test_a_wiped_live_login_is_replaced(fleet):
    """Claude Code empties the tokens after a refused refresh: renewing the
    active account is the way back, so the new login goes live."""
    s = fleet
    s._write_credentials(json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": ""}}))
    fake = FakeClaude("maya@example.com", uuid="uuid-1")
    assert login.renew(s, _target(s, "1"), runner=fake, interactive=False).live is True
    assert _fp(s._read_credentials()) == _fp(fake.creds)


def test_another_accounts_credential_in_the_live_store_is_left_alone(fleet, monkeypatch):
    """The config names #1 but the live store holds a login the profile
    endpoint says is someone else's: the switch that repairs that keeps a
    copy of it first, so a renewal saves #1's copy and leaves the store."""
    s = fleet
    foreign = _creds("someone-else")
    s._write_credentials(foreign)
    monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda token: {
        "uuid": "uuid-x", "email": "x@example.org", "organizationUuid": "",
    } if "someone-else" in token else None)
    fake = FakeClaude("maya@example.com", uuid="uuid-1")
    renewal = login.renew(s, _target(s, "1"), runner=fake, interactive=False)
    assert renewal.live is False
    assert s._read_credentials() == foreign
    assert _fp(s.read_account_credentials("1", "maya@example.com")) == _fp(fake.creds)


def test_a_key_saved_the_older_way_stays_live(fleet):
    """With the key in the live position (`--log-out-sessions`), the login
    is saved and the key stays where it is."""
    s = fleet
    key = "sk-ant-api03-" + "F" * 40
    s._write_credentials(key)
    fake = FakeClaude("maya@example.com", uuid="uuid-1")
    assert login.renew(s, _target(s, "1"), runner=fake, interactive=False).live is False
    assert s._read_credentials() == key
    assert _fp(s.read_account_credentials("1", "maya@example.com")) == _fp(fake.creds)


def test_under_an_active_key_it_does_what_add_does(fleet, monkeypatch):
    """With an API key active through the key line over #1, the login
    underneath is renewed, the key stays the active account, and the
    result says so the way `qswap add` does."""
    s = fleet
    s.add_account_from_token("sk-ant-api03-" + "K" * 40, slot=5, assume_yes=True)
    data = s._get_sequence_data()
    data["activeAccountNumber"] = 5
    s._write_json(s.sequence_file, data)
    monkeypatch.setattr(type(s), "helper_account", lambda self: "5")
    fake = FakeClaude("maya@example.com", uuid="uuid-1")
    renewal = login.renew(s, _target(s, "1"), runner=fake, interactive=False)
    assert renewal.live is True and renewal.key == "5"
    assert _fp(s._read_credentials()) == _fp(fake.creds)
    assert s._get_sequence_data()["activeAccountNumber"] == 5
    note = login.key_note(s, renewal)
    assert note.startswith("#5 api-key-5 is still the active API key; Claude Code sessions use it")
    assert note.endswith("To use this login: qswap switch 1")


# -- a sign-in that lands elsewhere, or does not finish --------------------------------------


def _unchanged(s):
    assert _fp(s.read_account_credentials("2", "maya@northwind.example.com")) == _fp(
        _creds("two-old", login_left=2 * DAY))
    assert _fp(s._read_credentials()) == _fp(_creds("one"))


def test_a_sign_in_as_another_saved_account_saves_nothing(fleet, block_real_keychain):
    s = fleet
    fake = FakeClaude("maya@example.com", uuid="uuid-1")
    with pytest.raises(LoginError) as e:
        login.renew(s, _target(s), runner=fake, interactive=False)
    assert str(e.value) == (
        "The browser signed in as #1 personal, not #2 work (maya@northwind.example.com), so "
        "nothing was saved. Sign out of claude.ai in the browser, or switch it to "
        "maya@northwind.example.com, and run it again: qswap login 2. To renew #1 personal "
        "instead: qswap login 1"
    )
    _unchanged(s)
    assert _profiles_left(s) == [".lock"]
    assert _keychain_items_for(block_real_keychain, fake.profile) == []


def test_a_sign_in_as_an_unknown_account_saves_nothing(fleet):
    s = fleet
    with pytest.raises(LoginError) as e:
        login.renew(s, _target(s), runner=FakeClaude("someone@example.org", uuid="uuid-x"),
                    interactive=False)
    assert str(e.value).startswith(
        "The browser signed in as someone@example.org, not #2 work (maya@northwind.example.com), "
        "so nothing was saved. Sign out of claude.ai in the browser")
    assert "instead" not in str(e.value)
    _unchanged(s)


def test_the_same_address_in_another_organization_saves_nothing(temp_home, platform):
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._init_sequence_file()
    _seed(s, 3, "maya@northwind.example.com", _creds("three"), org="org-northwind", alias="team")
    fake = FakeClaude("maya@northwind.example.com", org="", org_name="")
    with pytest.raises(LoginError) as e:
        login.renew(s, _target(s, "3"), runner=fake, interactive=False)
    assert str(e.value) == (
        "The browser signed in as maya@northwind.example.com (personal), not #3 team "
        "(maya@northwind.example.com), so nothing was saved. Sign out of claude.ai in the "
        "browser, or switch it to maya@northwind.example.com in Northwind, and run it "
        "again: qswap login 3"
    )
    assert _fp(s.read_account_credentials("3", "maya@northwind.example.com")) == _fp(_creds("three"))


@pytest.mark.parametrize("run, words", [
    (LoginRun(1, "Login failed: access denied."),
     "claude auth login failed (exit code 1): Login failed: access denied. Nothing was saved "
     "for #2 work; try again: qswap login 2"),
    (LoginRun(None, "timed out", timed_out=True),
     "The browser sign-in for #2 work did not finish within 5m, so nothing was saved. "
     "Try again with more time: qswap login 2 --timeout 600"),
    (LoginRun(None, "claude is not on PATH"),
     "Could not run claude auth login (claude is not on PATH), so nothing was saved. "
     "Install Claude Code or put it on PATH, then run: qswap login 2"),
], ids=["exit-1", "timeout", "no-claude"])
def test_a_sign_in_that_fails_saves_nothing_and_cleans_up(fleet, block_real_keychain, run, words):
    s = fleet
    fake = FakeClaude("maya@northwind.example.com", returncode=run.returncode,
                      timed_out=run.timed_out, detail=run.detail)
    with pytest.raises(LoginError) as e:
        login.renew(s, _target(s), runner=fake, interactive=False)
    assert str(e.value) == words
    _unchanged(s)
    assert _profiles_left(s) == [".lock"]
    assert _keychain_items_for(block_real_keychain, fake.profile) == []


@pytest.mark.parametrize("raised", [KeyboardInterrupt(), RuntimeError("boom")], ids=["cancel", "crash"])
def test_a_cancel_or_a_crash_cleans_up_and_frees_the_next_sign_in(fleet, block_real_keychain, raised):
    s = fleet
    fake = FakeClaude("maya@northwind.example.com", raises=raised)
    with pytest.raises(type(raised)):
        login.renew(s, _target(s), runner=fake, interactive=False)
    _unchanged(s)
    assert _profiles_left(s) == [".lock"]
    assert _keychain_items_for(block_real_keychain, fake.profile) == []
    # The sign-in lock went with it.
    login.renew(s, _target(s), runner=FakeClaude("maya@northwind.example.com"), interactive=False)


def test_a_sign_in_that_leaves_no_account_or_no_login(fleet):
    s = fleet
    with pytest.raises(LoginError, match="left no signed-in account"):
        login.renew(s, _target(s), runner=FakeClaude(write=False), interactive=False)
    with pytest.raises(LoginError, match="left no login to save"):
        login.renew(s, _target(s), runner=FakeClaude("maya@northwind.example.com", write_creds=False),
                    interactive=False)
    _unchanged(s)


def test_a_partial_login_is_not_saved(fleet):
    s = fleet
    half = json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-half", "expiresAt": 1}})
    with pytest.raises(LoginError, match="no complete login"):
        login.renew(s, _target(s), runner=FakeClaude("maya@northwind.example.com", creds=half),
                    interactive=False)
    _unchanged(s)


def test_leftovers_of_a_crash_go_at_the_next_sign_in(fleet, block_real_keychain):
    s = fleet
    root = login.sign_in_root(s)
    left = root / "profile-crashed"
    left.mkdir(parents=True)
    (left / ".credentials.json").write_text(_creds("left"), encoding="utf-8")
    service = keychain_service_name(str(left))
    if block_real_keychain is not None:
        block_real_keychain.set_password(service, macos_keychain.keychain_account_name(), _creds("left"))
    login.renew(s, _target(s), runner=FakeClaude("maya@northwind.example.com"), interactive=False)
    assert not left.exists()
    if Platform.detect() == Platform.MACOS:
        assert _keychain_items_for(block_real_keychain, left) == []


def test_a_keychain_item_that_cannot_be_deleted_keeps_its_directory(fleet, block_real_keychain, monkeypatch):
    """The directory's path is the only way to find the item again, so it
    stays, emptied, until a later sign-in can delete both."""
    s = fleet
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))
    root = login.sign_in_root(s)
    left = root / "profile-stuck"
    left.mkdir(parents=True)
    (left / ".credentials.json").write_text(_creds("left"), encoding="utf-8")
    real_delete = macos_keychain.delete_password

    def locked(service, account):
        raise macos_keychain.KeychainError("locked")

    monkeypatch.setattr(macos_keychain, "delete_password", locked)
    assert login.remove_profile(left) is False
    assert left.is_dir() and list(left.iterdir()) == []
    monkeypatch.setattr(macos_keychain, "delete_password", real_delete)
    login.sweep(root)
    assert not left.exists()


def test_one_sign_in_at_a_time(fleet):
    s = fleet
    root = login.sign_in_root(s)
    root.mkdir(parents=True, exist_ok=True)
    held = FileLock(root / ".lock")
    assert held.acquire(timeout=1)
    try:
        fake = FakeClaude("maya@northwind.example.com")
        with pytest.raises(LoginError, match="Another sign-in is waiting for the browser"):
            login.renew(s, _target(s), runner=fake, interactive=False)
        assert fake.calls == []
    finally:
        held.release()


# -- which accounts ---------------------------------------------------------------------------


def test_refuses_an_api_key_account(fleet):
    s = fleet
    s.add_account_from_token("sk-ant-api03-" + "A" * 40, slot=5, assume_yes=True)
    with pytest.raises(ValidationError) as e:
        login.resolve(s, "5")
    assert str(e.value) == (
        "#5 api-key-5 is an API-key account, which has no sign-in to renew; to replace "
        "its key: qswap add-token KEY --slot 5"
    )


def test_refuses_a_setup_token_account(fleet):
    s = fleet
    s.add_account_from_token("sk-ant-oat01-" + "S" * 40, slot=6, assume_yes=True)
    with pytest.raises(ValidationError) as e:
        login.resolve(s, "6")
    assert str(e.value) == (
        "#6 setup-token-6 holds a setup-token, which a sign-in does not renew; make a new "
        "one with claude setup-token, then: qswap add-token TOKEN --slot 6"
    )


def test_refuses_a_codex_account(fleet):
    s = fleet
    codex = s.backup_dir / "codex"
    codex.mkdir(exist_ok=True)
    (codex / "sequence.json").write_text(json.dumps({"accounts": {
        "1": {"email": "maya@openai.example.com", "alias": "gpt"}}}), encoding="utf-8")
    with pytest.raises(ValidationError) as e:
        login.resolve(s, "gpt")
    assert str(e.value) == (
        "gpt is a Codex account (experimental), and qswap login renews Claude Code logins; "
        "sign it in again with: qswap codex login"
    )
    with pytest.raises(AccountNotFoundError):
        login.resolve(s, "nobody")


def test_refuses_inside_a_qswap_run_session_shell(fleet, monkeypatch):
    s = fleet
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(s.backup_dir / "sessions" / "2-x"))
    fake = FakeClaude("maya@northwind.example.com")
    with pytest.raises(SwitchError, match="inside a qswap run session profile"):
        login.renew(s, Target("2", "maya@northwind.example.com", "", "#2 work"), runner=fake)
    assert fake.calls == []


def test_resolves_by_number_alias_or_email(fleet):
    s = fleet
    for identifier in ("2", "work", "maya@northwind.example.com"):
        t = login.resolve(s, identifier)
        assert (t.number, t.email, t.label) == ("2", "maya@northwind.example.com", "#2 work")


@pytest.fixture
def mixed(temp_home, platform, monkeypatch):
    """#1 due in 2d, #2 in 20d, #3 needing re-login, #4 an API key, #5 a
    setup-token, #6 a login that records no deadline, #7 no saved login."""
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._init_sequence_file()
    _seed(s, 1, "a@example.com", _creds("a", login_left=2 * DAY), alias="soon")
    _seed(s, 2, "b@example.com", _creds("b", login_left=20 * DAY), alias="later")
    _seed(s, 3, "c@example.com", _creds("c", login_left=25 * DAY), alias="dead")
    s.add_account_from_token("sk-ant-api03-" + "A" * 40, slot=4, assume_yes=True)
    s.add_account_from_token("sk-ant-oat01-" + "S" * 40, slot=5, assume_yes=True)
    _seed(s, 6, "f@example.com", json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-f", "refreshToken": "sk-ant-ort01-f"}}))
    _seed(s, 7, "g@example.com", _creds("g"))
    s._delete_account_credentials("7", "g@example.com")
    monkeypatch.setattr(type(s), "relogin_reasons", lambda self: {"3": "its saved login was refused"})
    return s


def test_selection_due_all_and_order(mixed):
    s, now = mixed, time.time()
    every = login.subscription_logins(s)
    assert [t.number for t in every] == ["1", "2", "3", "6", "7"]
    due = login.due_first([t for t in every if t.due(now)], now)
    assert [t.number for t in due] == ["3", "7", "1"]
    assert [t.note(now) for t in due] == ["needs re-login", "needs re-login", "due in 1d 23h"]
    assert [t.why for t in due[:2]] == ["its saved login was refused", "no saved login"]
    assert login.due_note(None, False, now) == ""


def test_nothing_due_names_the_next_one(temp_home, platform):
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._init_sequence_file()
    _seed(s, 1, "a@example.com", _creds("a", login_left=26 * DAY), alias="personal")
    _seed(s, 2, "b@example.com", _creds("b", login_left=20 * DAY), alias="work")
    now = time.time()
    text = login.nothing_due_text(login.subscription_logins(s), now)
    assert text.startswith("No login is due within 7 days. The next is #2 work, due ")
    assert text.endswith(" · in 19d 23h; to renew it now: qswap login 2")
    assert login.nothing_due_text([], now).startswith("No subscription logins are managed")
    undated = [Target("4", "d@example.com", "", "#4 d@example.com")]
    assert login.nothing_due_text(undated, now).endswith("to renew one anyway: qswap login 4")


def test_the_live_login_counts_for_the_active_accounts_deadline(fleet):
    """A /login made in Claude Code moves the live login's deadline before
    the saved copy catches up: the later one counts."""
    s = fleet
    s._write_credentials(_creds("one-relogged", login_left=29 * DAY))
    (t,) = [t for t in login.subscription_logins(s) if t.number == "1"]
    assert t.expires_at > time.time() + 28 * DAY


# -- the child process ---------------------------------------------------------------------


def test_the_child_environment_points_at_the_profile_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-not-for-the-child")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")
    env = login.login_env(tmp_path / "profile-x")
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "profile-x")
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_SECURESTORAGE_CONFIG_DIR"):
        assert name not in env


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    """A program named `claude` that records its arguments and environment,
    prints a line and exits with $FAKE_CLAUDE_EXIT (sleeping $FAKE_CLAUDE_SLEEP first)."""
    if sys.platform == "win32":
        pytest.skip("the stand-in is a POSIX script; Windows runs the injected-runner tests")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "record.json"
    script = bin_dir / "claude"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys, time
        time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
        with open({str(record)!r}, "w") as f:
            json.dump({{"argv": sys.argv[1:], "env": dict(os.environ)}}, f)
        print("Opening the browser to sign in")
        print("Login failed: the browser closed", file=sys.stderr)
        sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
        """), encoding="utf-8")
    script.chmod(0o755)
    from ai_quota_swap import clis

    monkeypatch.setattr(clis, "find_cli", lambda name: str(script) if name == "claude" else None)
    return record


def test_the_real_runner_starts_claude_auth_login(stand_in, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-not-for-the-child")
    profile = tmp_path / "profile-x"
    profile.mkdir()
    run = REAL_RUN(profile, "maya@example.com", sso=True, interactive=False, timeout=30)
    assert run == LoginRun(0, "Login failed: the browser closed")
    seen = json.loads(stand_in.read_text(encoding="utf-8"))
    assert seen["argv"] == ["auth", "login", "--claudeai", "--email", "maya@example.com", "--sso"]
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == str(profile)
    assert "ANTHROPIC_API_KEY" not in seen["env"]


def test_the_real_runner_reports_the_exit_code_and_the_timeout(stand_in, tmp_path, monkeypatch):
    profile = tmp_path / "profile-x"
    profile.mkdir()
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "3")
    run = REAL_RUN(profile, "maya@example.com", interactive=False, timeout=30)
    assert run.returncode == 3 and not run.timed_out
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "0")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "5")
    started = time.monotonic()
    run = REAL_RUN(profile, "maya@example.com", interactive=False, timeout=0.5)
    assert run == LoginRun(None, "timed out", timed_out=True)
    assert time.monotonic() - started < 4


def test_the_real_runner_without_claude(tmp_path, monkeypatch):
    from ai_quota_swap import clis

    monkeypatch.setattr(clis, "find_cli", lambda name: None)
    assert REAL_RUN(tmp_path, "maya@example.com", interactive=False) == LoginRun(None, "claude is not on PATH")


# -- the command ------------------------------------------------------------------------------


def _main(argv, capsys):
    from tests.api_helpers import run_main

    code = 0
    try:
        run_main(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def cli_fleet(fleet, monkeypatch):
    """`fleet`, with every switcher the command builds over the same data,
    and the stand-in runner in place of the real one."""
    fakes: list = []

    def runner(profile, email, **kw):
        fake = FakeClaude(email, uuid={"maya@example.com": "uuid-1"}.get(email, "uuid-2"))
        fakes.append(fake)
        return fake(profile, email, **kw)

    monkeypatch.setattr(login, "run_claude_login", runner)
    return fleet, fakes


def test_qswap_login_n(cli_fleet, capsys):
    s, fakes = cli_fleet
    code, out, err = _main(["login", "work"], capsys)
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0] == "The browser opens to sign in #2 work (maya@northwind.example.com). It must be"
    assert "Renewed the login of #2 work; the next sign-in is due " in out
    assert fakes[0].calls[0]["interactive"] is False  # the suite has no terminal
    assert _fp(s.read_account_credentials("2", "maya@northwind.example.com")) == _fp(fakes[0].creds)


def test_qswap_login_renews_the_due_ones(cli_fleet, capsys):
    s, fakes = cli_fleet
    code, out, err = _main(["login"], capsys)
    assert code == 0, err
    assert out.splitlines()[0] == "Renewing 1 login, one after another: #2 work (due in 1d 23h)"
    assert [f.calls[0]["email"] for f in fakes] == ["maya@northwind.example.com"]


def test_qswap_login_all(cli_fleet, capsys):
    s, fakes = cli_fleet
    code, out, err = _main(["login", "--all", "--timeout", "60"], capsys)
    assert code == 0, err
    assert " ".join(out.split()).startswith(
        "Renewing 2 logins, one after another: #2 work (due in 1d 23h), #1 personal (due in 19d 23h)"
    )
    assert [f.calls[0]["timeout"] for f in fakes] == [60.0, 60.0]
    assert out.count("Renewed the login of") == 2


def test_qswap_login_with_nothing_due(temp_home, platform, capsys):
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._init_sequence_file()
    _seed(s, 1, "a@example.com", _creds("a", login_left=26 * DAY), alias="personal")
    code, out, _ = _main(["login"], capsys)
    assert code == 0
    assert out.startswith("No login is due within 7 days. The next is #1 personal, due ")
    assert "to renew it now: qswap login 1" in " ".join(out.split())


def test_qswap_login_usage_errors(cli_fleet, capsys):
    code, _, err = _main(["login", "2", "--all"], capsys)
    assert code == 64 and "give ACCOUNT or --all, not both" in err
    code, _, err = _main(["login", "--timeout", "0"], capsys)
    assert code == 64 and "must be more than 0 seconds" in err


def test_qswap_login_failure_and_refusal(cli_fleet, capsys, monkeypatch):
    s, _ = cli_fleet
    s.add_account_from_token("sk-ant-api03-" + "A" * 40, slot=5, assume_yes=True)
    code, out, err = _main(["login", "5"], capsys)
    assert code == 1
    assert "Error: #5 api-key-5 is an API-key account" in out + err
    monkeypatch.setattr(login, "run_claude_login",
                        lambda profile, email, **kw: FakeClaude(email, returncode=1)(profile, email, **kw))
    code, out, err = _main(["login", "2"], capsys)
    assert code == 1
    assert "claude auth login failed (exit code 1). Nothing was saved for #2 work" in " ".join((out + err).split())


def test_qswap_login_cancelled(cli_fleet, capsys, monkeypatch):
    monkeypatch.setattr(login, "run_claude_login", lambda profile, email, **kw: FakeClaude(
        email, raises=KeyboardInterrupt())(profile, email, **kw))
    code, out, _ = _main(["login", "2"], capsys)
    assert code == 130
    assert out.rstrip().endswith("Cancelled; nothing was saved for #2 work.")


def test_qswap_login_goes_on_after_one_fails(cli_fleet, capsys, monkeypatch):
    s, _ = cli_fleet

    def runner(profile, email, **kw):
        if email == "maya@northwind.example.com":
            return FakeClaude("someone@example.org")(profile, email, **kw)
        return FakeClaude(email, uuid="uuid-1")(profile, email, **kw)

    monkeypatch.setattr(login, "run_claude_login", runner)
    code, out, err = _main(["login", "--all"], capsys)
    assert code == 1
    assert "The browser signed in as someone@example.org" in " ".join(err.split())
    assert "Renewed the login of #1 personal" in out


def test_qswap_login_help(capsys):
    code, out, _ = _main(["login", "--help"], capsys)
    assert code == 0
    assert out.startswith("usage: qswap login [ACCOUNT] [--all] [--sso] [--timeout SECONDS]")
    assert "The browser must be signed in to claude.ai as that account, or signed out." in out
    assert all(len(line) <= 80 for line in out.splitlines())
    code, main, _ = _main(["--help"], capsys)
    assert "  login [ACCOUNT]       sign an account in again before its login runs out" in main


# -- the menu bar: Sign in again -------------------------------------------------------------

_NOW = 1_000_000.0


def _acc(number, email, *, alias="", left=None, kind="oauth", setup_token=False, set_aside=None, **kw):
    return AccountState("claude", number, email, alias=alias, kind=kind, setup_token=setup_token,
                        set_aside=set_aside, login_expires_at=None if left is None else _NOW + left, **kw)


def _menu_input(*accounts, codex=(), **kw) -> MenuInput:
    return MenuInput(
        claude=ProviderState("claude", tuple(accounts)),
        codex=ProviderState("codex", tuple(codex), managed=len(codex)),
        settings=MenuBarSettings(), now=_NOW, **kw,
    )


_ACCOUNTS = (
    _acc("1", "maya@example.com", alias="personal", left=20 * DAY, active=True),
    _acc("2", "maya@northwind.example.com", alias="work", left=2 * DAY),
    _acc("3", "sam@example.org", alias="night", left=13 * 3600),
    _acc("4", "side@example.org", alias="side", left=25 * DAY, set_aside="invalid_grant"),
    _acc("5", "api-key-5@token.local", kind="api_key"),
    _acc("6", "setup-token-6@token.local", setup_token=True),
    _acc("7", "old@example.org"),
)


def test_sign_in_again_lists_the_subscription_logins_due_first():
    menu = menubar.build_menu(_menu_input(*_ACCOUNTS))
    row = menu.find("Sign in again")
    assert row.text == "Sign in again (3 due)"
    assert [c.text for c in row.children] == [
        "#4 side · needs re-login",
        "#3 night · due in 13h 0m",
        "#2 work · due in 2d 0h",
        "#1 personal · due in 20d 0h",
        "#7 old@example.org",
    ]
    assert [c.action for c in row.children] == [Action("sign-in", "claude", n) for n in "43217"]
    assert [c.attention for c in row.children] == [True, False, False, False, False]
    assert row.children[1].tooltip == (
        "The browser opens to sign in #3 night (sam@example.org). It must be signed in to "
        "claude.ai as sam@example.org, or signed out. The active login is not touched."
    )
    # In the Manage section, after Add account; Refresh current credentials stays.
    manage = [r.text for r in menu.section("manage").rows]
    assert manage[:2] == ["Add account", "Sign in again (3 due)"]
    assert "Refresh current credentials" in manage


def test_an_account_with_no_saved_login_is_due_in_the_menu_too():
    from ai_quota_swap.display import Reading
    from ai_quota_swap.json_output import USAGE_NO_CREDENTIALS

    acc = _acc("7", "old@example.org", entry=Reading(sentinel=USAGE_NO_CREDENTIALS))
    row = menubar.build_menu(_menu_input(_ACCOUNTS[0], acc)).find("Sign in again")
    assert row.text == "Sign in again (1 due)"
    assert [c.text for c in row.children] == ["#7 old@example.org · needs re-login", "#1 personal · due in 20d 0h"]


def test_sign_in_again_with_nothing_due_and_with_nothing_to_list():
    row = menubar.build_menu(_menu_input(_ACCOUNTS[0])).find("Sign in again")
    assert row.text == "Sign in again" and len(row.children) == 1
    assert menubar.build_menu(_menu_input(_ACCOUNTS[4], _ACCOUNTS[5])).find("Sign in again") is None


def test_sign_in_again_names_the_provider_beside_codex():
    codex = (AccountState("codex", "1", "x@example.com"),)
    row = menubar.build_menu(_menu_input(_ACCOUNTS[1], codex=codex)).find("Sign in again")
    assert [c.text for c in row.children] == ["Claude #2 work · due in 2d 0h"]


def test_while_one_runs_it_waits_and_nothing_can_be_clicked():
    menu = menubar.build_menu(_menu_input(*_ACCOUNTS, login_busy="2"))
    rows = menu.find("Sign in again").children
    assert "#2 work · waiting for the browser…" in [r.text for r in rows]
    assert all(r.action is None for r in rows)
    ran = []
    inp = _menu_input(*_ACCOUNTS, login_busy="2")
    assert not menubar.sign_in(inp, Action("sign-in", "claude", "3"), run=lambda n, who: ran.append(n))
    assert ran == []


def test_a_click_hands_the_account_to_the_runner():
    ran = []
    inp = _menu_input(*_ACCOUNTS)
    assert menubar.sign_in(inp, Action("sign-in", "claude", "3"),
                           run=lambda n, who: ran.append((n, who)) or True)
    assert ran == [("3", "#3 night")]
    assert not menubar.sign_in(inp, Action("sign-in", "claude", "5"), run=lambda n, who: True)
    assert not menubar.sign_in(inp, Action("sign-in", "claude", "9"), run=lambda n, who: True)
    assert "sign-in" in menubar.ACTION_KINDS


class _FakeRenew:
    def __init__(self, answer):
        self.answer = answer
        self.calls: list = []

    def __call__(self, switcher, number, *, save_lock=None):
        self.calls.append((switcher, number, save_lock))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class _Labels:
    def label(self, number, email=None, *, long=True):
        return {"5": "#5 ci"}.get(str(number), f"#{number}")


def _login_runner(answer, lock=None):
    queued, notes = [], []
    renew = _FakeRenew(answer)
    runner = menubar_engines.LoginRunner(renew, notes.append, start=queued.append, lock=lock)
    return runner, renew, queued, notes


def test_the_runner_runs_one_sign_in_at_a_time_and_reports_it():
    import threading

    lock = threading.Lock()
    renewal = Renewal("3", "#3 night", _NOW + 30 * DAY)
    runner, renew, queued, notes = _login_runner(renewal, lock=lock)
    assert runner.run("sw", "3", who="#3 night")
    assert runner.busy() == "3"
    assert not runner.run("sw", "2", who="#2 work")  # one at a time
    queued[0]()
    assert renew.calls == [("sw", "3", lock)]  # the save lock, not held around the wait
    assert runner.busy() is None
    assert notes[0].body.startswith("Renewed the login of #3 night; the next sign-in is due ")


def test_the_runner_reports_a_failure_in_the_command_lines_words():
    runner, _, queued, notes = _login_runner(LoginError(
        "The browser signed in as #1 personal, not #3 night (sam@example.org), so\nnothing was saved."))
    runner.run("sw", "3", who="#3 night")
    queued[0]()
    assert notes[0].body == (
        "The browser signed in as #1 personal, not #3 night (sam@example.org), so nothing was saved."
    )
    runner, _, queued, notes = _login_runner(RuntimeError("boom"))
    runner.run("sw", "3", who="#3 night")
    queued[0]()
    assert notes[0].body == "Could not renew the login of #3 night: RuntimeError: boom"
    assert runner.busy() is None


def test_the_runner_says_when_a_key_stays_active():
    runner, _, queued, notes = _login_runner(Renewal("1", "#1 personal", None, live=True, key="5"))
    runner.run(_Labels(), "1", who="#1 personal")
    queued[0]()
    assert notes[0].body == (
        "Renewed the login of #1 personal. #5 ci is still the active API key; Claude Code "
        "sessions use it, not this login. To use this login: qswap switch 1"
    )


def test_the_app_wires_sign_in_again():
    source = (Path(menubar.__file__).parent / "menubar_app.py").read_text(encoding="utf-8")
    assert "def _do_sign_in(" in source
    assert "functools.partial(login.renew_account, interactive=False)" in source
    assert "login_busy=self._login_runner.busy()" in source


# -- the notices before a deadline ----------------------------------------------------------


def _notices(accounts, said=None, *, now=_NOW, days=3, both=False):
    return menubar_notify.login_notices(accounts, now=now, notify_days=days, said=said or {}, both=both)


def test_one_notice_inside_the_window_and_one_in_the_last_day():
    acc = _acc("2", "maya@northwind.example.com", alias="work", left=2 * DAY + 3600)
    notes, said = _notices([acc])
    assert [n.body for n in notes] == [
        "The login of #2 work runs out in 2d 1h ("
        + oauth.login_expiry_clock(_NOW + 2 * DAY + 3600, _NOW)
        + "). Renew it with Sign in again in the menu, or run: qswap login 2"
    ]
    assert said == {"claude:2": {"deadline": int(_NOW + 2 * DAY + 3600), "said": ["window"]}}
    # The next reads say nothing more until the last day.
    assert _notices([acc], said, now=_NOW + 60)[0] == []
    later = _NOW + DAY + 7200  # 23h left
    notes, said = _notices([acc], said, now=later)
    assert len(notes) == 1 and "runs out in 23h 0m" in notes[0].body
    assert said["claude:2"]["said"] == ["last-day", "window"]
    assert _notices([acc], said, now=later + 600)[0] == []


def test_a_restart_does_not_repeat_them(tmp_path):
    acc = _acc("2", "maya@northwind.example.com", alias="work", left=2 * DAY)
    path = tmp_path / "menubar_state.json"
    notes, said = _notices([acc])
    assert len(notes) == 1
    state = MenuBarState(login_notices=said, extra={"futureKey": 1})
    state.save(path)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = MenuBarState.load(path)
    assert again.extra == {"futureKey": 1}
    assert _notices([acc], again.login_notices, now=_NOW + 3600)[0] == []
    assert MenuBarState.load(tmp_path / "missing.json").login_notices == {}


def test_a_renewed_login_starts_fresh():
    acc = _acc("2", "maya@northwind.example.com", alias="work", left=12 * 3600)
    _notes, said = _notices([acc])
    assert said["claude:2"]["said"] == ["last-day", "window"]  # first seen in the last day: one notice
    renewed = _acc("2", "maya@northwind.example.com", alias="work", left=29 * DAY)
    assert _notices([renewed], said)[0] == []
    # Four weeks on, the new deadline gets its own notices.
    notes, _ = _notices([renewed], said, now=_NOW + 27 * DAY)
    assert len(notes) == 1


def test_who_gets_none():
    accounts = [
        _acc("4", "side@example.org", left=DAY + 3600, set_aside="invalid_grant"),
        _acc("5", "api-key-5@token.local", kind="api_key", left=DAY),
        _acc("6", "setup-token-6@token.local", setup_token=True, left=DAY),
        _acc("7", "old@example.org"),
        _acc("8", "gone@example.org", left=-60),
        _acc("9", "far@example.org", left=10 * DAY),
        AccountState("codex", "1", "x@example.com", login_expires_at=_NOW + DAY),
    ]
    assert _notices(accounts)[0] == []


def test_notify_days_zero_turns_them_off_and_old_records_go():
    acc = _acc("2", "maya@northwind.example.com", alias="work", left=DAY + 60)
    old = {"claude:9": {"deadline": int(_NOW - 8 * DAY), "said": ["window"]}, "junk": 1}
    notes, said = _notices([acc], old, days=0)
    assert notes == [] and said == {}


def test_the_provider_is_named_beside_codex():
    acc = _acc("2", "maya@northwind.example.com", alias="work", left=2 * DAY)
    (note,), _ = _notices([acc], both=True)
    assert note.body.startswith("The login of Claude #2 work runs out in")


# -- the setting ---------------------------------------------------------------------------


def test_the_setting(tmp_path):
    from ai_quota_swap.settings import load_login_settings, set_setting, setting_spec

    assert load_login_settings(tmp_path).notify_days == 3
    assert set_setting(tmp_path, "login.notifyDays", "0") == 0
    assert load_login_settings(tmp_path).notify_days == 0
    (tmp_path / "settings.json").write_text(json.dumps({"login": {"notifyDays": 99}}), encoding="utf-8")
    assert load_login_settings(tmp_path).notify_days == 30
    (tmp_path / "settings.json").write_text(json.dumps({"login": {"notifyDays": "soon"}}), encoding="utf-8")
    assert load_login_settings(tmp_path).notify_days == 3
    assert setting_spec("login.notifyDays").default == 3


def test_qswap_config_sets_it(temp_home, capsys):
    code, out, err = _main(["config", "set", "login.notifyDays", "5"], capsys)
    assert code == 0, err
    code, out, _ = _main(["config", "get", "login.notifyDays"], capsys)
    assert out.strip() == "5"
    code, _, err = _main(["config", "set", "login.notifyDays", "31"], capsys)
    assert code == 1 and "between 0 and 30" in err


def test_a_new_login_the_keychain_will_not_give_up_saves_nothing(fleet, monkeypatch):
    s = fleet
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))

    def locked(service, account):
        raise macos_keychain.KeychainError("locked")

    fake = FakeClaude("maya@northwind.example.com", write_creds=False)

    def runner(profile, email, **kw):
        run = fake(profile, email, **kw)
        monkeypatch.setattr(macos_keychain, "get_password", locked)
        return run

    with pytest.raises(LoginError) as e:
        login.renew(s, _target(s), runner=runner, interactive=False)
    assert str(e.value) == (
        "The new login could not be read from the Keychain (it is locked or busy), so "
        "nothing was saved. Once it answers, try again: qswap login 2"
    )
