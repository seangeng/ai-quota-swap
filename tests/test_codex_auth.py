"""Codex login file: claims, identity, storage mode, locks, the atomic write."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from ai_quota_swap import paths
from ai_quota_swap.codex import auth as codex_auth
from ai_quota_swap.exceptions import CodexLoginError, LockError
from tests.codex_helpers import AUTH, PROFILE, LOGIN_A, make_jwt, make_login, write_live


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "codex-home"
    h.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(h))
    return h


class TestHome:
    def test_codex_home_follows_the_env_var(self, home):
        assert paths.get_codex_home() == home.resolve()
        assert paths.get_codex_auth_path() == home.resolve() / "auth.json"
        assert codex_auth.codex_home() == home.resolve()

    def test_default_home_without_the_env_var(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert paths.get_codex_home() == tmp_path / ".codex"
        assert paths.get_default_codex_home() == tmp_path / ".codex"

    def test_empty_env_var_means_unset(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CODEX_HOME", "")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert paths.get_codex_home() == tmp_path / ".codex"

    def test_a_missing_codex_home_is_refused_like_codex_does(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nope"))
        with pytest.raises(CodexLoginError, match="not an existing directory"):
            codex_auth.codex_home()

    def test_data_dir_is_under_the_backup_root(self, tmp_path):
        assert paths.get_codex_data_dir(tmp_path) == tmp_path / "codex"


class TestJwt:
    def test_decodes_the_payload_without_verifying(self):
        token = make_jwt({"email": "a@example.com", "exp": 5})
        assert codex_auth.decode_jwt_claims(token) == {"email": "a@example.com", "exp": 5}

    @pytest.mark.parametrize("token", [
        None, 5, "", "abc", "a.b", "a..c", "a.!!!.c", "a.bm90IGpzb24.c",
        # valid base64 of a JSON list, not an object
        f"a.{__import__('base64').urlsafe_b64encode(b'[1]').decode().rstrip('=')}.c",
    ])
    def test_malformed_tokens_decode_to_none(self, token):
        assert codex_auth.decode_jwt_claims(token) is None


class TestParseLogin:
    def test_identity_from_the_tokens(self):
        exp = time.time() + 5000
        raw = make_login(**LOGIN_A, plan="plus", exp=exp)
        login = codex_auth.parse_login(raw)
        assert login.email == "a@example.com"
        assert login.user_id == "user-a"
        assert login.account_id == "acct-a"
        assert login.identity == ("user-a", "acct-a")
        assert login.plan_type == "plus"
        assert login.access_expires_at == pytest.approx(exp)
        assert login.raw is raw
        assert login.auth_mode == "chatgpt"

    def test_legacy_record_without_auth_mode_is_a_chatgpt_login(self):
        login = codex_auth.parse_login(make_login(**LOGIN_A, legacy=True))
        assert login.auth_mode is None
        assert login.identity == ("user-a", "acct-a")

    def test_legacy_record_with_an_api_key_is_api_key_auth(self):
        raw = make_login(**LOGIN_A, legacy=True, api_key="sk-test-not-real")
        with pytest.raises(CodexLoginError, match="API key"):
            codex_auth.parse_login(raw)

    def test_chatgpt_mode_with_an_exchanged_key_is_still_chatgpt(self):
        """0.164's browser login can store an exchanged key beside auth_mode
        "chatgpt"; the explicit mode wins."""
        login = codex_auth.parse_login(make_login(**LOGIN_A, api_key="sk-test-not-real"))
        assert login.account_id == "acct-a"

    def test_api_key_mode_is_refused(self):
        raw = json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-test"}).encode()
        with pytest.raises(CodexLoginError, match="API-key login"):
            codex_auth.parse_login(raw)

    def test_other_modes_are_refused(self):
        raw = json.dumps({"personal_access_token": "x", "tokens": None}).encode()
        with pytest.raises(CodexLoginError, match="personal access token"):
            codex_auth.parse_login(raw)
        raw = json.dumps({"auth_mode": "agentIdentity"}).encode()
        with pytest.raises(CodexLoginError, match="agentIdentity"):
            codex_auth.parse_login(raw)

    @pytest.mark.parametrize("raw, message", [
        (b"", "not valid JSON"),
        (b"{not json", "not valid JSON"),
        (b"[1, 2]", "not a JSON object"),
        (b'{"auth_mode": "chatgpt"}', "no ChatGPT tokens"),
        (b'{"tokens": {"id_token": "x"}, "last_refresh": "t"}', "no access token"),
    ])
    def test_malformed_records(self, raw, message):
        with pytest.raises(CodexLoginError, match=message):
            codex_auth.parse_login(raw)

    def test_a_record_without_last_refresh_is_refused(self):
        with pytest.raises(CodexLoginError, match="last_refresh"):
            codex_auth.parse_login(make_login(**LOGIN_A, last_refresh=None))

    def test_email_falls_back_to_the_access_token_profile(self):
        raw = make_login(**LOGIN_A, id_token_claims={AUTH: {"chatgpt_user_id": "user-a"}})
        login = codex_auth.parse_login(raw)
        assert login.email == "a@example.com"
        assert login.user_id == "user-a"

    def test_a_malformed_id_token_falls_back_to_the_access_token(self):
        record = json.loads(make_login(**LOGIN_A))
        record["tokens"]["id_token"] = "garbage"
        login = codex_auth.parse_login(json.dumps(record).encode())
        assert login.identity == ("user-a", "acct-a")
        assert login.email == "a@example.com"

    def test_account_id_falls_back_to_the_claims(self):
        record = json.loads(make_login(**LOGIN_A))
        del record["tokens"]["account_id"]
        login = codex_auth.parse_login(json.dumps(record).encode())
        assert login.account_id == "acct-a"

    def test_tokens_without_identity_are_refused(self):
        record = json.loads(make_login(**LOGIN_A))
        record["tokens"]["id_token"] = make_jwt({"exp": 1})
        record["tokens"]["access_token"] = make_jwt({"exp": 1})
        with pytest.raises(CodexLoginError, match="email"):
            codex_auth.parse_login(json.dumps(record).encode())

    def test_an_unparseable_exp_reads_as_unknown(self):
        record = json.loads(make_login(**LOGIN_A))
        record["tokens"]["access_token"] = make_jwt({
            AUTH: {"chatgpt_user_id": "user-a"}, PROFILE: {"email": "a@example.com"},
            "exp": "soon",
        })
        assert codex_auth.parse_login(json.dumps(record).encode()).access_expires_at is None

    def test_try_parse_login(self):
        assert codex_auth.try_parse_login(None) is None
        assert codex_auth.try_parse_login("") is None
        assert codex_auth.try_parse_login("nope") is None
        assert codex_auth.try_parse_login(make_login(**LOGIN_A).decode()).email == "a@example.com"


class TestStorageMode:
    def test_file_by_default(self, home):
        assert codex_auth.configured_store_mode(home) == "file"
        codex_auth.ensure_file_storage(home)  # no raise

    @pytest.mark.parametrize("mode", ["keyring", "auto"])
    def test_keyring_modes_are_refused(self, home, mode):
        (home / "config.toml").write_text(f'model = "x"\ncli_auth_credentials_store = "{mode}"\n')
        with pytest.raises(CodexLoginError, match="file storage only") as exc:
            codex_auth.ensure_file_storage(home)
        assert "\n" not in str(exc.value)

    def test_ephemeral_is_refused(self, home):
        (home / "config.toml").write_text('cli_auth_credentials_store = "ephemeral"\n')
        with pytest.raises(CodexLoginError, match="memory only"):
            codex_auth.ensure_file_storage(home)

    def test_explicit_file_mode_is_fine(self, home):
        (home / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
        codex_auth.ensure_file_storage(home)

    def test_malformed_config_reads_as_file(self, home):
        (home / "config.toml").write_text("this is = = not toml")
        assert codex_auth.configured_store_mode(home) == "file"

    def test_missing_auth_json_with_a_keychain_item_is_refused(self, home, block_real_keychain, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        block_real_keychain.data[(codex_auth.KEYRING_SERVICE, codex_auth.keyring_account(home))] = "x"
        with pytest.raises(CodexLoginError, match="keyring"):
            codex_auth.ensure_file_storage(home)

    def test_a_keychain_item_does_not_matter_while_auth_json_exists(self, home, block_real_keychain, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        block_real_keychain.data[(codex_auth.KEYRING_SERVICE, codex_auth.keyring_account(home))] = "x"
        write_live(home, make_login(**LOGIN_A))
        codex_auth.ensure_file_storage(home)

    def test_missing_auth_json_with_the_secrets_file_is_refused(self, home):
        (home / "secrets").mkdir()
        (home / "secrets" / "codex_auth.age").write_bytes(b"x")
        with pytest.raises(CodexLoginError, match="keyring"):
            codex_auth.ensure_file_storage(home)

    def test_keyring_account_hashes_the_canonical_home(self, home, tmp_path):
        import hashlib

        expected = hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:16]
        assert codex_auth.keyring_account(home) == f"cli|{expected}"
        link = tmp_path / "link-home"
        link.symlink_to(home)
        assert codex_auth.keyring_account(link) == codex_auth.keyring_account(home)


class TestReadAuthBytes:
    def test_missing_is_none(self, home):
        assert codex_auth.read_auth_bytes(home / "auth.json") is None

    def test_reads_bytes_verbatim(self, home):
        raw = make_login(**LOGIN_A)
        assert codex_auth.read_auth_bytes(write_live(home, raw)) == raw

    def test_a_half_written_file_is_retried(self, home, monkeypatch):
        path = home / "auth.json"
        raw = make_login(**LOGIN_A)
        reads = iter([b"", raw])
        monkeypatch.setattr(codex_auth, "_READ_RETRY_DELAY_S", 0)
        monkeypatch.setattr(Path, "read_bytes", lambda self: next(reads))
        assert codex_auth.read_auth_bytes(path) == raw


class TestLocksAndWrite:
    def test_commit_lock_sits_beside_the_resolved_file(self, home, tmp_path):
        assert codex_auth.commit_lock_path(home / "auth.json") == home / "auth.json.lock"
        elsewhere = tmp_path / "store"
        elsewhere.mkdir()
        (elsewhere / "creds").write_bytes(b"{}")
        (home / "auth.json").symlink_to(elsewhere / "creds")
        # Rust's with_extension("json.lock") on the resolved path.
        assert codex_auth.commit_lock_path(home / "auth.json") == elsewhere / "creds.json.lock"

    def test_relative_symlinks_resolve_against_the_link_dir(self, home):
        (home / "real.json").write_bytes(b"{}")
        (home / "auth.json").symlink_to("real.json")
        assert codex_auth.resolve_auth_file(home / "auth.json") == home / "real.json"

    def test_symlink_loops_are_refused(self, home):
        (home / "auth.json").symlink_to(home / "auth.json")
        with pytest.raises(CodexLoginError, match="too many symlinks"):
            codex_auth.resolve_auth_file(home / "auth.json")

    def test_locks_are_created_0600_and_never_deleted(self, home):
        with codex_auth.codex_auth_locks(home):
            assert (home / "auth.lock").exists()
            assert (home / "auth.json.lock").exists()
        assert (home / "auth.lock").exists()
        assert (home / "auth.json.lock").exists()
        if sys.platform != "win32":
            assert stat.S_IMODE((home / "auth.json.lock").stat().st_mode) == 0o600

    def test_existing_lock_files_keep_their_contents(self, home):
        (home / "auth.lock").write_bytes(b"")
        before = (home / "auth.lock").stat().st_ino
        with codex_auth.codex_auth_locks(home):
            pass
        assert (home / "auth.lock").stat().st_ino == before

    def test_a_held_auth_lock_is_a_retryable_lock_error(self, home, monkeypatch):
        """Another process (Codex) holding auth.lock: nothing changes."""
        import fcntl

        monkeypatch.setattr(codex_auth, "AUTH_LOCK_TIMEOUT_S", 0.2)
        (home / "auth.lock").touch()
        with open(home / "auth.lock", "r+") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            with pytest.raises(LockError, match="auth.lock"):
                with codex_auth.codex_auth_locks(home):
                    pytest.fail("entered while Codex held the lock")

    def test_a_held_commit_lock_releases_the_first_lock(self, home, monkeypatch):
        import fcntl

        monkeypatch.setattr(codex_auth, "COMMIT_LOCK_TIMEOUT_S", 0.2)
        (home / "auth.json.lock").touch()
        with open(home / "auth.json.lock", "r+") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            with pytest.raises(LockError, match="auth.json.lock"):
                with codex_auth.codex_auth_locks(home):
                    pass
        # auth.lock was released on the way out.
        with open(home / "auth.lock", "r+") as probe:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_write_is_atomic_0600_and_keeps_a_symlink(self, home, tmp_path):
        target = tmp_path / "store" / "auth.json"
        target.parent.mkdir()
        target.write_bytes(b"old")
        (home / "auth.json").symlink_to(target)
        raw = make_login(**LOGIN_A)
        codex_auth.write_auth_bytes(home / "auth.json", raw)
        assert (home / "auth.json").is_symlink()
        assert target.read_bytes() == raw
        if sys.platform != "win32":
            assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_check_replaceable_refuses_hard_links_and_read_only(self, home, tmp_path):
        path = write_live(home, make_login(**LOGIN_A))
        codex_auth.check_replaceable(path)
        os.link(path, tmp_path / "second-name")
        with pytest.raises(CodexLoginError, match="hard-linked"):
            codex_auth.check_replaceable(path)
        os.unlink(tmp_path / "second-name")
        path.chmod(0o400)
        try:
            if os.geteuid() != 0:
                with pytest.raises(CodexLoginError, match="read-only"):
                    codex_auth.check_replaceable(path)
        finally:
            path.chmod(0o600)

    def test_check_replaceable_allows_a_missing_file(self, home):
        codex_auth.check_replaceable(home / "auth.json")


def test_token_fingerprint_is_stable_and_opaque():
    fp = codex_auth.token_fingerprint("secret-token")
    assert fp == codex_auth.token_fingerprint("secret-token")
    assert fp.startswith("sha256:") and "secret-token" not in fp
