"""``qswap api org add|list|remove`` and the Admin key store (admin_orgs.py)."""

from __future__ import annotations

import base64
import io
import json
import logging
import sys

import pytest

from ai_quota_swap import admin_api, api_cli
from ai_quota_swap.admin_orgs import (
    ENV_VAR,
    OrgError,
    OrgRegistry,
    default_org_name,
    looks_like_admin_key,
    normalize_org_name,
    purge_secrets,
)
from ai_quota_swap.credentials import SECURITY_SERVICE
from ai_quota_swap.models import Platform

from tests.admin_fakes import (
    ADMIN_KEY,
    KEY_A,
    KEY_B,
    OTHER_ADMIN_KEY,
    FakeAnthropic,
    error_response,
    json_response,
    key_item,
    keys_page,
    org_info,
)
from tests.api_helpers import (
    data_root,
    make_switcher,
    pin_platform,
    record,
    run_main,
    seed_api_key,
    seed_oauth,
)


@pytest.fixture
def linux(temp_home, monkeypatch):
    pin_platform(monkeypatch, Platform.LINUX)
    monkeypatch.delenv(ENV_VAR, raising=False)
    admin_api._memory.clear()
    return temp_home


@pytest.fixture
def anthropic(monkeypatch):
    fake = FakeAnthropic({
        "/v1/organizations/me": [json_response(org_info())],
        "/v1/organizations/api_keys": [json_response(keys_page([
            key_item("apikey_01A", "Dev laptop", KEY_A),
            key_item("apikey_01B", "CI", KEY_B),
        ]))],
    })
    monkeypatch.setattr(admin_api, "send", fake)
    return fake


def _stdin(monkeypatch, text: str, tty: bool = False):
    stream = io.StringIO(text)
    stream.isatty = lambda: tty  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stream)


def _secret_file(name: str):
    return data_root() / "credentials" / f".admin-org-{name}.enc"


class TestKeyShape:
    def test_admin_keys(self):
        assert looks_like_admin_key(ADMIN_KEY)
        assert looks_like_admin_key(f"  {ADMIN_KEY}\n")

    @pytest.mark.parametrize("value", [
        None, "", KEY_A, "sk-ant-oat01-" + "x" * 40, "sk-ant-admin01-short",
        "sk-ant-admin01-" + "ab cd" * 10, "not-a-key",
    ])
    def test_others(self, value):
        assert not looks_like_admin_key(value)

    def test_names(self):
        assert normalize_org_name("Acme") == "acme"
        assert default_org_name("Acme Corp, Inc.") == "acme-corp-inc"
        assert default_org_name("") == "default"
        for bad in ("", "a b", "-x", "x" * 41, "env"):
            with pytest.raises(OrgError):
                normalize_org_name(bad)


class TestAdd:
    def test_from_the_hidden_prompt(self, linux, anthropic, monkeypatch, capsys, caplog):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        _stdin(monkeypatch, "", tty=True)
        prompts = []
        monkeypatch.setattr(api_cli.getpass, "getpass", lambda p: prompts.append(p) or ADMIN_KEY)
        caplog.set_level(logging.DEBUG, logger="ai-quota-swap")
        run_main(["api", "org", "add"])
        out, err = capsys.readouterr()
        assert prompts == ["Admin API key (input hidden): "]
        assert "Registered organization 'acme-corp' (Acme Corp)." in out
        assert "Linked #3 api-key-3@token.local" in out and '"Dev laptop"' in out
        assert "manage your whole organization" in err
        for text in (out, err, caplog.text):
            assert ADMIN_KEY not in text
        # Stored as a 0600 file (Linux), the registry holds no key.
        assert base64.b64decode(_secret_file("acme-corp").read_text()).decode() == ADMIN_KEY
        registry_text = (data_root() / "api_orgs.json").read_text()
        assert ADMIN_KEY not in registry_text
        assert json.loads(registry_text)["orgs"]["acme-corp"]["storage"] == "file"
        if sys.platform != "win32":
            assert _secret_file("acme-corp").stat().st_mode & 0o777 == 0o600
        # The check was one GET to /me, then the key listing for linking.
        assert anthropic.paths() == ["/v1/organizations/me", "/v1/organizations/api_keys"]
        assert {c["method"] for c in anthropic.calls} == {"GET"}
        assert record(make_switcher(), 3)["apiKeyId"] == "apikey_01A"
        assert record(make_switcher(), 3)["apiOrg"] == "acme-corp"

    def test_from_stdin_with_a_name(self, linux, anthropic, monkeypatch, capsys):
        make_switcher()
        _stdin(monkeypatch, ADMIN_KEY + "\n")
        run_main(["api", "org", "add", "Acme"])
        out = capsys.readouterr().out
        assert "Registered organization 'acme'" in out
        assert "No API-key accounts are managed yet" in out
        assert OrgRegistry(data_root()).read_secret("acme") == ADMIN_KEY

    def test_refuses_a_key_that_is_not_an_admin_key(self, linux, anthropic, monkeypatch, capsys):
        make_switcher()
        _stdin(monkeypatch, KEY_A + "\n")
        with pytest.raises(SystemExit) as exc:
            run_main(["api", "org", "add", "acme"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        flat = " ".join(err.split())
        assert "not an Admin API key" in flat and "nothing was saved" in flat
        assert KEY_A not in err
        assert anthropic.calls == []
        assert not (data_root() / "api_orgs.json").exists()
        assert not _secret_file("acme").exists()

    def test_refuses_a_key_anthropic_rejects(self, linux, anthropic, monkeypatch, capsys):
        make_switcher()
        anthropic.routes["/v1/organizations/me"] = [error_response(401, "authentication_error", "invalid x-api-key")]
        _stdin(monkeypatch, ADMIN_KEY + "\n")
        with pytest.raises(SystemExit):
            run_main(["api", "org", "add", "acme"])
        err = capsys.readouterr().err
        assert "rejected the key (HTTP 401)" in err
        assert ADMIN_KEY not in err
        assert not _secret_file("acme").exists()

    def test_empty_input(self, linux, anthropic, monkeypatch, capsys):
        make_switcher()
        _stdin(monkeypatch, "")
        with pytest.raises(SystemExit):
            run_main(["api", "org", "add", "acme"])
        assert "no key was given" in capsys.readouterr().err

    def test_never_takes_the_key_from_the_environment(self, linux, anthropic, monkeypatch, capsys):
        make_switcher()
        monkeypatch.setenv(ENV_VAR, ADMIN_KEY)
        _stdin(monkeypatch, "")
        with pytest.raises(SystemExit):
            run_main(["api", "org", "add", "acme"])
        assert anthropic.calls == []
        assert not _secret_file("acme").exists()

    def test_no_argument_takes_a_key(self, linux, anthropic, monkeypatch, capsys):
        make_switcher()
        _stdin(monkeypatch, "")
        # A key passed as NAME is not a valid name, and is never treated as a key.
        with pytest.raises(SystemExit):
            run_main(["api", "org", "add", ADMIN_KEY])
        err = capsys.readouterr().err
        assert "not a usable name" in err
        assert anthropic.calls == []

    def test_duplicates(self, linux, anthropic, monkeypatch, capsys):
        make_switcher()
        _stdin(monkeypatch, ADMIN_KEY + "\n")
        run_main(["api", "org", "add", "acme"])
        _stdin(monkeypatch, ADMIN_KEY + "\n")
        with pytest.raises(SystemExit):
            run_main(["api", "org", "add", "acme"])
        assert "already registered" in capsys.readouterr().err
        _stdin(monkeypatch, OTHER_ADMIN_KEY + "\n")
        with pytest.raises(SystemExit):
            run_main(["api", "org", "add", "other"])  # same organization id
        assert "already registered as 'acme'" in capsys.readouterr().err
        assert not _secret_file("other").exists()


class TestList:
    def _register(self, monkeypatch):
        _stdin(monkeypatch, ADMIN_KEY + "\n")
        run_main(["api", "org", "add", "acme"])

    def test_text_and_json_never_print_the_key(self, linux, anthropic, monkeypatch, capsys):
        s = make_switcher()
        seed_api_key(s, 2, KEY_B)
        self._register(monkeypatch)
        capsys.readouterr()
        run_main(["api", "org", "list"])
        out = capsys.readouterr().out
        assert "acme" in out and "Acme Corp" in out and "linked: #2" in out
        assert ADMIN_KEY not in out
        run_main(["api", "org", "list", "--json"])
        doc = json.loads(capsys.readouterr().out)
        assert doc["organizations"][0]["name"] == "acme"
        assert doc["organizations"][0]["linkedAccounts"] == [2]
        assert ADMIN_KEY not in json.dumps(doc)
        assert ADMIN_KEY[15:] not in json.dumps(doc)

    def test_nothing_registered(self, linux, monkeypatch, capsys):
        make_switcher()
        run_main(["api", "org", "list"])
        assert "No organization registered" in capsys.readouterr().out
        monkeypatch.setenv(ENV_VAR, ADMIN_KEY)
        run_main(["api", "org", "list"])
        out = capsys.readouterr().out
        assert f"{ENV_VAR} is set" in out and ADMIN_KEY not in out


class TestRemove:
    def test_deletes_the_secret_and_the_links(self, linux, anthropic, monkeypatch, capsys):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        _stdin(monkeypatch, ADMIN_KEY + "\n")
        run_main(["api", "org", "add", "acme"])
        assert _secret_file("acme").exists()
        assert (data_root() / "cache" / "admin_api.json").exists()
        run_main(["api", "org", "remove", "acme"])
        out = capsys.readouterr().out
        assert "Removed organization 'acme'" in out and "Unlinked: #3" in out
        assert not _secret_file("acme").exists()
        assert OrgRegistry(data_root()).orgs() == []
        rec = record(make_switcher(), 3)
        assert not any(k in rec for k in ("apiKeyId", "apiOrg", "apiKeyHint"))
        cache = json.loads((data_root() / "cache" / "admin_api.json").read_text())
        assert "acme" not in cache["orgs"]

    def test_unknown_name(self, linux, capsys):
        make_switcher()
        with pytest.raises(SystemExit):
            run_main(["api", "org", "remove", "nope"])
        assert "no organization named 'nope'" in capsys.readouterr().err

    def test_bare_api_prints_help(self, linux, capsys):
        with pytest.raises(SystemExit) as exc:
            run_main(["api"])
        assert exc.value.code == 64  # a usage error: its help goes to stderr
        assert "org" in capsys.readouterr().err


class TestSecretStore:
    def test_macos_uses_the_keychain(self, temp_home, block_real_keychain):
        root = temp_home / "data"
        reg = OrgRegistry(root, platform=Platform.MACOS)
        org = reg.add("acme", ADMIN_KEY, "org-1", "Acme")
        assert org.storage == "keychain"
        assert block_real_keychain.data[(SECURITY_SERVICE, "admin-org-acme")] == ADMIN_KEY
        assert not reg.secret_path("acme").exists()
        assert OrgRegistry(root, platform=Platform.MACOS).read_secret("acme") == ADMIN_KEY
        assert ADMIN_KEY not in repr(org)
        known, gone = reg.remove("acme")
        assert known and gone
        assert (SECURITY_SERVICE, "admin-org-acme") not in block_real_keychain.data

    def test_macos_falls_back_to_a_file(self, temp_home, block_real_keychain, monkeypatch):
        from ai_quota_swap import macos_keychain

        def locked(*a, **k):
            raise macos_keychain.KeychainError("locked")

        monkeypatch.setattr(macos_keychain, "set_password", locked)
        reg = OrgRegistry(temp_home / "data", platform=Platform.MACOS)
        assert reg.add("acme", ADMIN_KEY, "org-1", "Acme").storage == "file"
        assert reg.secret_path("acme").exists()
        assert OrgRegistry(temp_home / "data", platform=Platform.MACOS).read_secret("acme") == ADMIN_KEY

    def test_environment_key_is_only_in_memory(self, temp_home):
        reg = OrgRegistry(temp_home / "data", platform=Platform.LINUX)
        (org,) = reg.active_orgs({ENV_VAR: ADMIN_KEY})
        assert org.storage == "environment" and not org.persistent
        assert org.admin_key() == ADMIN_KEY
        assert ADMIN_KEY not in repr(org)
        assert not (temp_home / "data").exists()
        assert reg.active_orgs({ENV_VAR: KEY_A}) == []  # not an Admin key: ignored
        reg.add("acme", OTHER_ADMIN_KEY, "org-1", "Acme")
        assert [o.name for o in reg.active_orgs({ENV_VAR: ADMIN_KEY})] == ["acme"]

    def test_purge_removes_stored_keys(self, temp_home, block_real_keychain):
        root = temp_home / "data"
        reg = OrgRegistry(root, platform=Platform.MACOS)
        reg.add("acme", ADMIN_KEY, "org-1", "Acme")
        assert purge_secrets(root, Platform.MACOS) == ["Admin key: acme"]
        assert (SECURITY_SERVICE, "admin-org-acme") not in block_real_keychain.data
