"""The provider seam (M1): contracts, registry, credential accessors, error base.

The engine used to parse stored credentials itself (``oauth.extract_oauth_data``
for the expiry, ``oauth.credential_fingerprint`` for quarantine identity). It
now asks its switcher. The tests here hold the new path to the old one, case by
case, including the malformed inputs where the two kinds of "no expiry" must
stay apart.
"""

from __future__ import annotations

import inspect
import json
import math
import typing
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_quota_swap import exceptions, oauth, providers
from ai_quota_swap.autoswitch import FRESHEN_BUFFER_MS, ErrorEvent, TickOutcome
from ai_quota_swap.exceptions import (
    ClaudeSwitchError,
    CredentialFormatError,
    ProviderError,
    ProviderUnavailableError,
    SwitchError,
)
from ai_quota_swap.json_output import error_envelope
from ai_quota_swap.providers import EngineHost, SurfaceHost
from ai_quota_swap.codex.switcher import CodexSwitcher
from ai_quota_swap.switcher import ClaudeAccountSwitcher
from tests.test_autoswitch import EngineHarness


# -- the contracts -----------------------------------------------------------------

#: Contract 1, spelled out so that renaming a member on either side, or adding
#: one to the Protocol without deciding it belongs there, fails a test.
ENGINE_HOST_MEMBERS = (
    "provider_id",
    "display_name",
    "cli_name",
    "backup_dir",
    "current_account_number",
    "has_live_login",
    "switchable_account_numbers",
    "disabled_account_numbers",
    "account_kind_for",
    "account_email",
    "account_identity",
    "backfill_account_uuid",
    "account_policies",
    "read_account_credentials",
    "credential_expires_at_ms",
    "credential_fingerprint",
    "live_session_pids_for",
    "consume_backup_grant",
    "usage_entries_by_account",
    "refresh_usage",
    "claim_usage_reset",
    "switch_to",
    "set_poll_policy_inputs",
    "clear_poll_policy_inputs",
    "reset_settings",
)

#: Contract 3.
SURFACE_HOST_MEMBERS = (
    "accounts_snapshot",
    "switch_to",
    "switch",
    "add_account",
    "remove_account",
    "set_account_disabled",
)

_DATA_MEMBERS = {"provider_id", "display_name", "cli_name", "backup_dir"}


def _protocol_members(proto: type) -> set[str]:
    getter = getattr(typing, "get_protocol_members", None)  # Python 3.13+
    return set(getter(proto) if getter else proto.__protocol_attrs__)


def _params(fn) -> list[tuple[str, object, object]]:
    return [
        (p.name, p.kind, p.default)
        for p in inspect.signature(fn).parameters.values()
    ]


@pytest.fixture
def switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    return ClaudeAccountSwitcher()


class TestContracts:
    def test_engine_host_declares_exactly_contract_one(self):
        assert _protocol_members(EngineHost) == set(ENGINE_HOST_MEMBERS)

    def test_surface_host_declares_exactly_contract_three(self):
        assert _protocol_members(SurfaceHost) == set(SURFACE_HOST_MEMBERS)

    @pytest.mark.parametrize("name", ENGINE_HOST_MEMBERS)
    def test_the_claude_switcher_has_every_engine_host_member(self, switcher, name):
        assert hasattr(switcher, name), f"ClaudeAccountSwitcher lacks {name}"

    @pytest.mark.parametrize("name", SURFACE_HOST_MEMBERS)
    def test_the_claude_switcher_has_every_surface_host_member(self, switcher, name):
        assert hasattr(switcher, name), f"ClaudeAccountSwitcher lacks {name}"

    @pytest.mark.parametrize(
        "proto, name",
        [(EngineHost, n) for n in ENGINE_HOST_MEMBERS if n not in _DATA_MEMBERS]
        + [(SurfaceHost, n) for n in SURFACE_HOST_MEMBERS],
    )
    def test_method_signatures_match_the_protocol(self, proto, name):
        """Same parameter names, kinds and defaults: a call written against
        the Protocol is a valid call on the Claude switcher."""
        assert _params(getattr(ClaudeAccountSwitcher, name)) == _params(
            getattr(proto, name)
        )

    def test_the_claude_switcher_is_both_hosts_at_runtime(self, switcher):
        assert isinstance(switcher, EngineHost)
        assert isinstance(switcher, SurfaceHost)

    def test_provider_identity(self, switcher):
        assert (switcher.provider_id, switcher.display_name, switcher.cli_name) == (
            "claude", "Claude Code", "claude",
        )
        assert isinstance(switcher.backup_dir, Path)


class TestCodexSwitcherFitsTheContracts:
    """The Codex switcher is its own class; it must fit both Protocols with
    the Claude switcher's exact signatures."""

    @pytest.fixture
    def codex(self, temp_home: Path) -> CodexSwitcher:
        return CodexSwitcher()

    def test_not_a_claude_subclass(self):
        assert not issubclass(CodexSwitcher, ClaudeAccountSwitcher)

    @pytest.mark.parametrize("name", ENGINE_HOST_MEMBERS + SURFACE_HOST_MEMBERS)
    def test_has_every_member(self, codex, name):
        assert hasattr(codex, name), f"CodexSwitcher lacks {name}"

    @pytest.mark.parametrize(
        "proto, name",
        [(EngineHost, n) for n in ENGINE_HOST_MEMBERS if n not in _DATA_MEMBERS]
        + [(SurfaceHost, n) for n in SURFACE_HOST_MEMBERS],
    )
    def test_method_signatures_match_the_protocol(self, proto, name):
        assert _params(getattr(CodexSwitcher, name)) == _params(getattr(proto, name))

    def test_is_both_hosts_at_runtime(self, codex):
        assert isinstance(codex, EngineHost)
        assert isinstance(codex, SurfaceHost)

    def test_provider_identity_and_data_root(self, codex, temp_home):
        assert (codex.provider_id, codex.display_name, codex.cli_name) == (
            "codex", "Codex", "codex",
        )
        claude_root = ClaudeAccountSwitcher().backup_dir
        assert codex.backup_dir == claude_root / "codex"

    def test_construction_writes_nothing(self, temp_home):
        CodexSwitcher()
        assert not (temp_home / ".ai-quota-swap" / "codex").exists()

    def test_claim_never_raises_and_run_sessions_are_the_next_milestone(self, codex):
        # M2b: spending a credit is implemented; an unknown account is an
        # auth_error with nothing sent (the network block would fail a send).
        from ai_quota_swap.resets import ClaimResult

        result = codex.claim_usage_reset("1", "g", "r")
        assert isinstance(result, ClaimResult) and result.result == "auth_error"
        assert codex.live_session_pids_for("1", "a@example.com") == []


# -- the registry ------------------------------------------------------------------


class TestRegistry:
    def test_provider_ids_in_order(self):
        assert providers.PROVIDERS == ("claude", "codex")
        assert providers.DEFAULT_PROVIDER == "claude"

    def test_make_switcher_builds_the_claude_switcher(self, temp_home):
        built = providers.make_switcher(debug=False)
        assert type(built) is ClaudeAccountSwitcher
        assert type(providers.make_switcher("claude")) is ClaudeAccountSwitcher

    def test_make_switcher_builds_the_codex_switcher(self, temp_home):
        with patch("ai_quota_swap.switcher.ClaudeAccountSwitcher") as cls:
            built = providers.make_switcher("codex", debug=False)
        assert type(built) is CodexSwitcher
        cls.assert_not_called()

    def test_an_unavailable_provider_is_refused_before_construction(self, monkeypatch):
        monkeypatch.setitem(providers._UNAVAILABLE, "codex", "not in this build")
        with patch("ai_quota_swap.codex.switcher.CodexSwitcher") as cls:
            with pytest.raises(ProviderUnavailableError) as exc:
                providers.make_switcher("codex", debug=True)
        assert str(exc.value) == "not in this build"
        assert isinstance(exc.value, ProviderError)
        cls.assert_not_called()

    def test_an_unknown_provider_is_a_programming_error(self):
        with pytest.raises(ValueError):
            providers.make_switcher("gemini")
        with pytest.raises(ValueError):
            providers.ensure_available("gemini")

    @pytest.mark.parametrize(
        "argv, expected",
        [
            ([], ("claude", [])),
            (["list"], ("claude", ["list"])),
            (["codex"], ("codex", [])),
            (["codex", "list", "--json"], ("codex", ["list", "--json"])),
            # Only argv[0] selects; `claude` is not a prefix.
            (["list", "codex"], ("claude", ["list", "codex"])),
            (["claude", "list"], ("claude", ["claude", "list"])),
            (["--json", "codex"], ("claude", ["--json", "codex"])),
        ],
    )
    def test_split_provider_prefix(self, argv, expected):
        assert providers.split_provider_prefix(argv) == expected

    def test_split_provider_prefix_does_not_alias_its_input(self):
        argv = ["list"]
        _provider, rest = providers.split_provider_prefix(argv)
        rest.append("--json")
        assert argv == ["list"]


# -- the credential accessors -----------------------------------------------------

#: FakeClock's default instant, in ms: EngineHarness's engine runs on it.
_NOW_MS = 1_000_000.0 * 1000


def _oauth(**fields) -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": "sk-x", **fields}})


#: (id, stored credential). Every shape the engine can read back from a slot.
CREDENTIAL_CASES = [
    ("empty", ""),
    ("not-json", "not json"),
    ("bare-api-key", "sk-ant-api03-abcdef"),
    ("json-null", "null"),
    ("json-list", "[]"),
    ("json-number", "5"),
    ("empty-object", "{}"),
    ("oauth-null", '{"claudeAiOauth": null}'),
    ("oauth-list", '{"claudeAiOauth": []}'),
    ("oauth-string", '{"claudeAiOauth": "x"}'),
    ("oauth-empty-object", '{"claudeAiOauth": {}}'),
    ("other-top-level-key", '{"accessToken": "a", "refreshToken": "r"}'),
    ("setup-token", _oauth(scopes=["user:inference"])),
    ("refresh-no-expiry", _oauth(refreshToken="rt")),
    ("expires-null", _oauth(refreshToken="rt", expiresAt=None)),
    ("expires-string", _oauth(refreshToken="rt", expiresAt="1")),
    ("expires-list", _oauth(refreshToken="rt", expiresAt=[1])),
    ("expires-true", _oauth(refreshToken="rt", expiresAt=True)),
    ("expires-false", _oauth(refreshToken="rt", expiresAt=False)),
    ("expires-zero", _oauth(refreshToken="rt", expiresAt=0)),
    ("expired", _oauth(refreshToken="rt", expiresAt=1)),
    ("expired-float", _oauth(refreshToken="rt", expiresAt=1.5)),
    ("negative", _oauth(refreshToken="rt", expiresAt=-5)),
    ("at-buffer-edge", _oauth(refreshToken="rt", expiresAt=_NOW_MS + FRESHEN_BUFFER_MS)),
    ("past-buffer-edge", _oauth(refreshToken="rt", expiresAt=_NOW_MS + FRESHEN_BUFFER_MS + 1)),
    ("fresh", _oauth(refreshToken="rt", expiresAt=99_999_999_999_000)),
    ("nan", '{"claudeAiOauth": {"refreshToken": "rt", "expiresAt": NaN}}'),
    ("infinity", '{"claudeAiOauth": {"refreshToken": "rt", "expiresAt": Infinity}}'),
    ("minus-infinity", '{"claudeAiOauth": {"refreshToken": "rt", "expiresAt": -Infinity}}'),
    ("empty-refresh-token", _oauth(refreshToken="", expiresAt=1)),
    ("non-string-refresh-token", _oauth(refreshToken=7, expiresAt=1)),
]
_IDS = [case_id for case_id, _ in CREDENTIAL_CASES]
_CREDS = [creds for _, creds in CREDENTIAL_CASES]


def _same(a, b) -> bool:
    """Equal and of the same type (True must stay a bool, 1 must not become 1.0);
    NaN equals NaN."""
    if type(a) is not type(b):
        return False
    if isinstance(a, float) and math.isnan(a):
        return math.isnan(b)
    return a == b


def _old_expiry_view(creds: str):
    """The engine's read of a stored credential before M1, verbatim
    (``_freshen_target``): ``"not-a-login"`` or the raw ``expiresAt``."""
    data = oauth.extract_oauth_data(creds)
    if not data:
        return "not-a-login"
    return data.get("expiresAt")


def _old_freshen_decision(creds: str, now_s: float) -> str:
    """``_freshen_target``'s credential handling before M1, verbatim, up to
    the refresh; ``"consume"`` where it went on to the consume gate."""
    if not creds:
        return "transient"
    data = oauth.extract_oauth_data(creds)
    if not data:
        return "invalid_grant"
    expires_at = data.get("expiresAt")
    now_ms = now_s * 1000
    near_expiry = (
        isinstance(expires_at, (int, float))
        and now_ms + FRESHEN_BUFFER_MS >= expires_at
    )
    if not near_expiry:
        return "ok"
    return "consume"


class TestCredentialExpiresAtMs:
    @pytest.mark.parametrize("creds", [c for c in _CREDS if c], ids=[i for i, c in CREDENTIAL_CASES if c])
    def test_matches_the_engines_old_read(self, switcher, creds):
        old = _old_expiry_view(creds)
        if old == "not-a-login":
            with pytest.raises(CredentialFormatError):
                switcher.credential_expires_at_ms(creds)
            return
        new = switcher.credential_expires_at_ms(creds)
        if isinstance(old, (int, float)):
            assert _same(new, old)
        else:
            assert new is None, "a non-numeric expiresAt reads as no expiry"

    def test_a_setup_token_is_a_login_without_expiry(self, switcher):
        """The distinction the single None of the design could not carry:
        a setup-token is usable (``ok``), garbage is a dead lineage."""
        assert switcher.credential_expires_at_ms(_oauth(scopes=["user:inference"])) is None
        with pytest.raises(CredentialFormatError):
            switcher.credential_expires_at_ms("{}")

    def test_the_format_error_is_a_provider_error(self, switcher):
        with pytest.raises(ProviderError):
            switcher.credential_expires_at_ms("not json")


class TestCredentialFingerprint:
    @pytest.mark.parametrize("creds", [*_CREDS, None], ids=[*_IDS, "none"])
    def test_matches_oauth_credential_fingerprint(self, switcher, creds):
        assert switcher.credential_fingerprint(creds) == oauth.credential_fingerprint(creds)

    def test_refresh_lineage_survives_access_token_rotation(self, switcher):
        first = _oauth(refreshToken="rt", expiresAt=1)
        rotated = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-new", "refreshToken": "rt", "expiresAt": 2,
        }})
        assert switcher.credential_fingerprint(first) == switcher.credential_fingerprint(rotated)
        assert switcher.credential_fingerprint(first).startswith("sha256:")


# -- the engine goes through the host ---------------------------------------------


@pytest.fixture
def harness(temp_home: Path) -> EngineHarness:
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.make_live("a@example.com", 1)
    return h


class TestEngineSitesBehaveAsBefore:
    @pytest.mark.parametrize("creds", _CREDS, ids=_IDS)
    def test_freshen_target_decides_exactly_as_before(self, harness, creds):
        gate_calls = []

        def gate(num, email, snapshot):
            gate_calls.append(snapshot)
            return oauth.RefreshOutcome(None, "transient")

        with patch.object(harness.switcher, "read_account_credentials", return_value=creds), \
             patch.object(harness.switcher, "consume_backup_grant", side_effect=gate):
            status = harness.engine._freshen_target("2", "b@example.com")

        new = "consume" if gate_calls else status
        assert new == _old_freshen_decision(creds, harness.clock.now)
        if gate_calls:
            assert gate_calls == [creds] and status == "transient"

    def test_freshen_target_reads_expiry_through_the_host(self, harness):
        """The site no longer parses credentials itself: what the host answers
        is what the engine acts on."""
        with patch.object(harness.switcher, "read_account_credentials", return_value="opaque"), \
             patch.object(harness.switcher, "credential_expires_at_ms", return_value=None) as expiry:
            assert harness.engine._freshen_target("2", "b@example.com") == "ok"
        expiry.assert_called_once_with("opaque")
        with patch.object(harness.switcher, "read_account_credentials", return_value="opaque"), \
             patch.object(harness.switcher, "credential_expires_at_ms",
                          side_effect=CredentialFormatError("no")):
            assert harness.engine._freshen_target("2", "b@example.com") == "invalid_grant"

    @pytest.mark.parametrize("creds", _CREDS, ids=_IDS)
    def test_quarantine_persists_the_same_fingerprint(self, harness, creds):
        with patch.object(harness.switcher, "read_account_credentials", return_value=creds):
            harness.engine._quarantine("2", "b@example.com", "invalid_grant")
        stored = harness.state()["quarantine"]["2"]["refreshTokenFingerprint"]
        assert stored == (oauth.credential_fingerprint(creds) if creds else None)

    @pytest.mark.parametrize("creds", _CREDS, ids=_IDS)
    def test_release_keeps_an_unchanged_credential_and_drops_a_replaced_one(
        self, harness, creds
    ):
        with patch.object(harness.switcher, "read_account_credentials", return_value=creds):
            harness.engine._quarantine("2", "b@example.com", "invalid_grant")
            kept = harness.engine._release_recovered_quarantines(harness.state())
        assert "2" in kept["quarantine"]

        replaced = _oauth(refreshToken="a-new-lineage", expiresAt=1)
        with patch.object(harness.switcher, "read_account_credentials", return_value=replaced):
            released = harness.engine._release_recovered_quarantines(harness.state())
        assert "2" not in released.get("quarantine", {})
        assert harness.kinds()[-1] == "account-unquarantined"

    def test_quarantine_reads_the_fingerprint_through_the_host(self, harness):
        with patch.object(harness.switcher, "read_account_credentials", return_value="opaque"), \
             patch.object(harness.switcher, "credential_fingerprint", return_value="fp:x") as fp:
            harness.engine._quarantine("2", "b@example.com", "invalid_grant")
        fp.assert_called_once_with("opaque")
        assert harness.state()["quarantine"]["2"]["refreshTokenFingerprint"] == "fp:x"


# -- the error base ----------------------------------------------------------------


def _claude_error_classes() -> list[type]:
    return [
        obj for obj in vars(exceptions).values()
        if isinstance(obj, type) and issubclass(obj, ClaudeSwitchError)
    ]


class TestProviderErrorBase:
    def test_every_claude_error_is_a_provider_error(self):
        classes = _claude_error_classes()
        assert len(classes) > 10  # the whole family, not just the root
        for cls in classes:
            assert issubclass(cls, ProviderError), cls

    def test_the_existing_hierarchy_is_unchanged_beneath_claude_switch_error(self):
        assert ClaudeSwitchError.__bases__ == (ProviderError,)
        assert SwitchError.__bases__ == (ClaudeSwitchError,)
        assert exceptions.CredentialReadError.__mro__[1:3] == (
            exceptions.CredentialError, ClaudeSwitchError,
        )

    def test_provider_errors_outside_claude(self):
        for cls in (
            ProviderUnavailableError, CredentialFormatError,
            exceptions.FeatureUnavailableError, exceptions.CodexLoginError,
        ):
            assert issubclass(cls, ProviderError)
            assert not issubclass(cls, ClaudeSwitchError)

    def test_the_json_envelope_still_names_the_switch_error(self):
        """Why the new base is not called SwitchError: this name is shown."""
        assert error_envelope(SwitchError("boom"))["error"]["type"] == "SwitchError"

    def test_the_engine_tick_reports_a_provider_error(self, harness):
        with patch.object(
            harness.switcher, "current_account_number",
            side_effect=ProviderUnavailableError("Codex support is not available yet"),
        ):
            outcome = harness.tick_with_usage({})
        assert outcome is TickOutcome.ERROR
        errors = [e for e in harness.events if isinstance(e, ErrorEvent)]
        assert [e.message for e in errors] == ["Codex support is not available yet"]
        assert errors[0].transient is True

    def test_the_tui_reports_a_provider_error(self):
        from ai_quota_swap.tui.data import run_action

        def action():
            raise ProviderUnavailableError("Codex support is not available yet")

        result = run_action(action)
        assert result.ok is False
        assert result.output.strip() == "Error: Codex support is not available yet"
