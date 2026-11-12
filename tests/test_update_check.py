"""Tests for update_check module."""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from ai_quota_swap.update_check import (
    CACHE_TTL,
    FAILURE_TTL,
    PYPI_URL,
    _detect_install_method,
    check_for_update,
    held_back_release_notice,
    run_self_upgrade,
)


def _make_pypi_response(version: str) -> MagicMock:
    data = json.dumps({"info": {"version": version}}).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _uv_pip_list(rows: list[dict]) -> MagicMock:
    """A completed `uv pip list --outdated --format json` run."""
    return MagicMock(returncode=0, stdout=json.dumps(rows))


_OUTDATED = [{"name": "ai-quota-swap", "version": "0.27.2", "latest_version": "0.28.0"}]


def _on_disk(version: str, source: str | None = None) -> dict:
    """What _installed_identity_on_disk reads after an upgrade."""
    return {"version": version, "source": source}


def _write_cache(path, version, timestamp=None):
    """Write a cache file in the shared cache format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "timestamp": timestamp if timestamp is not None else time.time(),
        "data": version,
    }))


class TestCheckForUpdate:
    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    def test_newer_version_available(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_pypi_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "0.4.0" in result
        assert "0.3.2" in result

    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    def test_already_on_latest(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_pypi_response("0.3.2")

        result = check_for_update("0.3.2")

        assert result is None

    @patch("ai_quota_swap.update_check.urllib.request.urlopen", side_effect=OSError("network error"))
    def test_network_error_returns_none_and_caches(self, mock_urlopen, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", cache_path)

        result = check_for_update("0.3.2")

        assert result is None
        assert cache_path.exists()
        cache = json.loads(cache_path.read_text())
        assert cache["data"] is None

    def test_pypi_404_is_silent_and_cached(self, tmp_path, monkeypatch, capsys):
        """PyPI's JSON endpoint answers 404 for a name it does not know, as it
        did for this one before 0.1.0. That must read as "no update" with
        nothing printed, and be cached as a failure so the next command does
        not ask again."""
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", cache_path)
        monkeypatch.setattr("ai_quota_swap.update_check._detect_install_method", lambda: None)
        not_found = urllib.error.HTTPError(PYPI_URL, 404, "Not Found", None, None)

        with patch(
            "ai_quota_swap.update_check.urllib.request.urlopen", side_effect=not_found
        ) as mock_urlopen:
            result = check_for_update("0.1.0")

        assert result is None
        assert mock_urlopen.call_args.args[0].full_url == (
            "https://pypi.org/pypi/ai-quota-swap/json"
        )
        assert json.loads(cache_path.read_text())["data"] is None
        assert capsys.readouterr() == ("", "")

    def test_uv_install_absent_from_index_and_pypi_404_is_silent(
        self, tmp_path, monkeypatch, capsys
    ):
        """A `uv tool install git+...` install: uv cannot resolve the package
        against the index and PyPI 404s. Still no hint and no output."""
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("ai_quota_swap.update_check._detect_install_method", lambda: "uv")
        monkeypatch.setattr("ai_quota_swap.update_check._read_receipt", lambda: {})
        monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
        not_found = urllib.error.HTTPError(PYPI_URL, 404, "Not Found", None, None)
        uv_failed = subprocess.CalledProcessError(2, ["uv", "pip", "list"])

        with patch(
            "ai_quota_swap.update_check.subprocess.run", side_effect=uv_failed
        ), patch(
            "ai_quota_swap.update_check.urllib.request.urlopen", side_effect=not_found
        ) as mock_urlopen:
            result = check_for_update("0.1.0")

        assert result is None
        mock_urlopen.assert_called_once()
        assert capsys.readouterr() == ("", "")

    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    def test_fresh_error_cache_skips_network(self, mock_urlopen, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, None)
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", cache_path)

        result = check_for_update("0.3.2")

        mock_urlopen.assert_not_called()
        assert result is None

    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    def test_failed_lookup_is_retried_after_an_hour(self, mock_urlopen, tmp_path, monkeypatch):
        # A cached failure outlives a flaky moment, not a working day.
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, None, timestamp=time.time() - FAILURE_TTL - 1)
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", cache_path)
        mock_urlopen.return_value = _make_pypi_response("0.4.0")

        result = check_for_update("0.3.2")

        mock_urlopen.assert_called_once()
        assert result is not None
        assert "0.4.0" in result

    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    def test_successful_lookup_outlives_the_failure_backoff(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, "0.4.0", timestamp=time.time() - FAILURE_TTL - 1)
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", cache_path)

        result = check_for_update("0.3.2")

        mock_urlopen.assert_not_called()
        assert result is not None
        assert "0.4.0" in result

    def test_fresh_cache_no_network(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, "0.5.0")
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", cache_path)

        with patch("ai_quota_swap.update_check.urllib.request.urlopen") as mock_urlopen:
            result = check_for_update("0.3.2")
            mock_urlopen.assert_not_called()

        assert result is not None
        assert "0.5.0" in result

    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    def test_stale_cache_fetches_from_pypi(self, mock_urlopen, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, "0.3.0", timestamp=time.time() - CACHE_TTL - 1)
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", cache_path)
        mock_urlopen.return_value = _make_pypi_response("0.4.0")

        result = check_for_update("0.3.2")

        mock_urlopen.assert_called_once()
        assert result is not None
        assert "0.4.0" in result


class TestCheckForUpdatePrereleases:
    """ai-quota-swap ships every cycle as a pre-release first, so the version
    cli.main hands to check_for_update is routinely something like 0.27.0b1."""

    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    def test_prerelease_is_told_about_later_release(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_pypi_response("0.28.0")

        result = check_for_update("0.27.0b1")

        assert result is not None
        assert "0.28.0" in result
        assert "0.27.0b1" in result

    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    def test_prerelease_is_told_about_its_own_final_release(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_pypi_response("0.27.0")

        result = check_for_update("0.27.0b1")

        assert result is not None
        assert "0.27.0" in result

    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    def test_prerelease_is_told_about_later_prerelease(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_pypi_response("0.27.0rc1")

        result = check_for_update("0.27.0b2")

        assert result is not None
        assert "0.27.0rc1" in result

    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    def test_final_release_is_not_pushed_onto_a_prerelease(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_pypi_response("0.28.0b1")

        assert check_for_update("0.27.0") is None

    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    def test_unparseable_version_stays_silent(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_pypi_response("0.28.0")

        assert check_for_update("main") is None


class TestDetectInstallMethod:
    def _set_prefix(self, monkeypatch, prefix: str) -> None:
        monkeypatch.setattr("ai_quota_swap.update_check.sys.prefix", prefix)
        # Clear env vars by default so path-based detection runs in isolation.
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("PIPX_HOME", raising=False)

    def test_uv_tool_default_path(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/.local/share/uv/tools/ai-quota-swap")
        assert _detect_install_method() == "uv"

    def test_pipx_default_path(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/.local/pipx/venvs/ai-quota-swap")
        assert _detect_install_method() == "pipx"

    def test_non_adjacent_uv_tools_does_not_match(self, monkeypatch):
        # Both segments present but not adjacent — must not false-positive.
        self._set_prefix(monkeypatch, "/home/me/projects/uv/some-tools/.venv")
        assert _detect_install_method() is None

    def test_non_adjacent_pipx_venvs_does_not_match(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/repos/pipx-clone/venvs-of-mine/.venv")
        assert _detect_install_method() is None

    def test_source_checkout_returns_none(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/code/ai-quota-swap/.venv")
        assert _detect_install_method() is None

    def test_mixed_case_path_detected(self, monkeypatch):
        # Lowercasing should make matching case-insensitive (e.g. Windows).
        self._set_prefix(monkeypatch, "/Home/Me/.local/share/UV/Tools/ai-quota-swap")
        assert _detect_install_method() == "uv"

    def test_uv_tool_dir_env_with_prefix_under_it(self, monkeypatch, tmp_path):
        custom_root = tmp_path / "uv-tools"
        prefix = custom_root / "ai-quota-swap"
        monkeypatch.setattr("ai_quota_swap.update_check.sys.prefix", str(prefix))
        monkeypatch.setenv("UV_TOOL_DIR", str(custom_root))
        monkeypatch.delenv("PIPX_HOME", raising=False)
        assert _detect_install_method() == "uv"

    def test_uv_tool_dir_env_set_but_prefix_elsewhere(self, monkeypatch, tmp_path):
        custom_root = tmp_path / "uv-tools"
        # Prefix lives somewhere else entirely — env var alone must not trigger.
        monkeypatch.setattr(
            "ai_quota_swap.update_check.sys.prefix", str(tmp_path / "some-project" / ".venv")
        )
        monkeypatch.setenv("UV_TOOL_DIR", str(custom_root))
        monkeypatch.delenv("PIPX_HOME", raising=False)
        assert _detect_install_method() is None

    def test_pipx_home_env_with_prefix_under_it(self, monkeypatch, tmp_path):
        custom_root = tmp_path / "pipx-home"
        prefix = custom_root / "venvs" / "ai-quota-swap"
        monkeypatch.setattr("ai_quota_swap.update_check.sys.prefix", str(prefix))
        monkeypatch.setenv("PIPX_HOME", str(custom_root))
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        assert _detect_install_method() == "pipx"


class TestCheckForUpdateMessage:
    @patch("ai_quota_swap.update_check.sys.platform", "linux")
    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_detected_method_non_windows_suggests_qswap_upgrade(
        self, mock_run, tmp_path, monkeypatch
    ):
        # uv/pipx on macOS/Linux: qswap upgrade actually upgrades, so advertise it.
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("ai_quota_swap.update_check.sys.prefix", str(tmp_path))
        monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
        monkeypatch.setattr("ai_quota_swap.update_check._detect_install_method", lambda: "uv")
        mock_run.return_value = _uv_pip_list([{"name": "ai-quota-swap", "latest_version": "0.4.0"}])

        result = check_for_update("0.3.2")

        assert result is not None
        assert "qswap upgrade" in result
        assert "uv tool upgrade" not in result

    @patch("ai_quota_swap.update_check.sys.platform", "win32")
    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    def test_detected_method_windows_suggests_direct_command(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        # Windows: qswap upgrade only prints, so point at the real command.
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("ai_quota_swap.update_check._detect_install_method", lambda: "pipx")
        mock_urlopen.return_value = _make_pypi_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "pipx upgrade ai-quota-swap" in result
        assert "qswap upgrade" not in result

    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    def test_unknown_method_suggests_qswap_instructions(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        # Unknown install method: qswap upgrade can only show instructions.
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("ai_quota_swap.update_check._detect_install_method", lambda: None)
        mock_urlopen.return_value = _make_pypi_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "qswap upgrade` for upgrade instructions" in result
        assert "uv tool upgrade" not in result
        assert "pipx upgrade" not in result


class _UvInstall:
    """A uv tool install under tmp_path, with the receipt written per test."""

    @pytest.fixture(autouse=True)
    def _uv_install(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("ai_quota_swap.update_check.sys.prefix", str(tmp_path))
        monkeypatch.setattr("ai_quota_swap.update_check.sys.platform", "linux")
        monkeypatch.setattr("ai_quota_swap.update_check._detect_install_method", lambda: "uv")
        monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
        # PyPI is unreachable unless a test says otherwise, so a uv failure
        # that falls back to it cannot leave the sandbox.
        monkeypatch.setattr(
            "ai_quota_swap.update_check.urllib.request.urlopen",
            MagicMock(side_effect=OSError("network error")),
        )

    @staticmethod
    def _write_receipt(
        prefix, options: str = "", requirements: str = '{ name = "ai-quota-swap" }'
    ) -> None:
        (prefix / "uv-receipt.toml").write_text(
            f"[tool]\nrequirements = [{requirements}]\n\n[tool.options]\n{options}\n"
        )

    @staticmethod
    def _uv_command(mock_run) -> list[str]:
        mock_run.assert_called_once()
        return mock_run.call_args.args[0]


class TestUvInstall(_UvInstall):
    """uv installs ask uv, not PyPI, which release is installable: a tool
    installed with --exclude-newer (a release cooldown) keeps that setting in
    the receipt, `uv tool upgrade` reapplies it, and a release younger than
    the cooldown is invisible to uv. Advertising it would send the user to
    `qswap upgrade` only to read "Nothing to upgrade"."""

    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_asks_uv_with_the_receipt_span(self, mock_run, mock_urlopen, tmp_path):
        self._write_receipt(
            tmp_path, 'exclude-newer = "2026-01-01T00:00:00Z"\nexclude-newer-span = "P14D"'
        )
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        result = check_for_update("0.27.2")

        assert result is not None
        assert "(0.28.0)" in result
        assert "qswap upgrade" in result
        cmd = self._uv_command(mock_run)
        assert cmd[:5] == ["uv", "pip", "list", "--outdated", "--format"]
        assert cmd[-2:] == ["--exclude-newer", "P14D"]
        mock_urlopen.assert_not_called()

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_absolute_cutoff_without_span(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer = "2026-01-01T00:00:00Z"')
        mock_run.return_value = _uv_pip_list([])

        check_for_update("0.27.2")

        assert self._uv_command(mock_run)[-2:] == ["--exclude-newer", "2026-01-01T00:00:00Z"]

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_env_var_overrides_receipt_and_is_passed_explicitly(
        self, mock_run, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("UV_EXCLUDE_NEWER", "1 week")
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = _uv_pip_list([])

        check_for_update("0.27.2")

        assert self._uv_command(mock_run)[-2:] == ["--exclude-newer", "1 week"]
        assert "UV_EXCLUDE_NEWER" not in mock_run.call_args.kwargs["env"]

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_asks_uv_from_the_tool_directory(self, mock_run, tmp_path):
        # uv reads the uv.toml of whatever project the shell is in, and a
        # project's own exclude-newer would hide releases that `uv tool
        # upgrade`, which ignores project config, is happy to install.
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        check_for_update("0.27.2")

        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path)

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_no_cooldown_passes_no_cutoff(self, mock_run, tmp_path):
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        result = check_for_update("0.27.2")

        assert result is not None
        assert "--exclude-newer" not in self._uv_command(mock_run)

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_nothing_outdated_is_quiet_and_cached(self, mock_run, tmp_path):
        mock_run.return_value = _uv_pip_list(
            [{"name": "platformdirs", "latest_version": "4.13.5"}]
        )

        assert check_for_update("0.27.2") is None
        cache = json.loads((tmp_path / "cache.json").read_text())
        assert cache["data"] == "0.27.2"

    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    @patch("ai_quota_swap.update_check.subprocess.run", side_effect=FileNotFoundError)
    def test_uv_missing_without_cooldown_asks_pypi(self, mock_run, mock_urlopen, tmp_path):
        # `qswap auto --once` under cron or launchd runs without uv on PATH.
        # Nothing hides releases from this install, so PyPI's answer is the
        # one uv would have given, and it is cached like any other.
        mock_urlopen.return_value = _make_pypi_response("0.28.0")

        result = check_for_update("0.27.2")

        assert result is not None
        assert "(0.28.0)" in result
        mock_urlopen.assert_called_once()
        cache = json.loads((tmp_path / "cache.json").read_text())
        assert cache["data"] == "0.28.0"

    @patch("ai_quota_swap.update_check.subprocess.run", side_effect=FileNotFoundError)
    def test_uv_missing_with_cooldown_is_quiet_and_retried(self, mock_run, tmp_path):
        # PyPI would advertise the release the cooldown hides, so the run
        # stays quiet. Caching that for a day would silence the interactive
        # runs that follow, which do have uv, so nothing is written.
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')

        assert check_for_update("0.27.2") is None
        assert not (tmp_path / "cache.json").exists()

        mock_run.side_effect = None
        mock_run.return_value = _uv_pip_list(_OUTDATED)
        assert check_for_update("0.27.2") is not None

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_uv_network_failure_with_cooldown_backs_off_for_an_hour(self, mock_run, tmp_path):
        # Only a missing uv retries immediately. A timeout or a failed
        # resolve under a cooldown would otherwise cost every following
        # command up to the 10 s subprocess timeout.
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.side_effect = subprocess.TimeoutExpired("uv", 10)

        assert check_for_update("0.27.2") is None
        assert check_for_update("0.27.2") is None
        assert mock_run.call_count == 1
        cache = json.loads((tmp_path / "cache.json").read_text())
        assert cache["data"] is None

        _write_cache(tmp_path / "cache.json", None, timestamp=time.time() - FAILURE_TTL - 1)
        mock_run.side_effect = None
        mock_run.return_value = _uv_pip_list(_OUTDATED)
        assert check_for_update("0.27.2") is not None
        assert mock_run.call_count == 2

    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_uv_failure_without_cooldown_asks_pypi(self, mock_run, mock_urlopen, tmp_path):
        mock_run.side_effect = subprocess.CalledProcessError(2, "uv")
        mock_urlopen.return_value = _make_pypi_response("0.28.0")

        assert check_for_update("0.27.2") is not None

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_uv_and_pypi_both_failing_is_quiet_and_cached(self, mock_run, tmp_path):
        mock_run.side_effect = subprocess.CalledProcessError(2, "uv")

        assert check_for_update("0.27.2") is None
        cache = json.loads((tmp_path / "cache.json").read_text())
        assert cache["data"] is None

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_fresh_cache_skips_uv(self, mock_run, tmp_path):
        _write_cache(tmp_path / "cache.json", "0.28.0")

        result = check_for_update("0.27.2")

        assert result is not None
        mock_run.assert_not_called()

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_cache_in_an_unknown_shape_is_refetched(self, mock_run, tmp_path):
        _write_cache(tmp_path / "cache.json", {"latest": "0.28.0"})
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        assert check_for_update("0.27.2") is not None
        mock_run.assert_called_once()

    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    def test_pipx_install_asks_pypi(self, mock_urlopen, mock_run, monkeypatch):
        monkeypatch.setattr("ai_quota_swap.update_check._detect_install_method", lambda: "pipx")
        mock_urlopen.return_value = _make_pypi_response("0.28.0")

        assert check_for_update("0.27.2") is not None
        mock_run.assert_not_called()


class TestHeldBackReleaseNotice(_UvInstall):
    """`uv tool upgrade` prints "Nothing to upgrade" both when the install is
    current and when the newest release is younger than the cooldown, so
    `qswap upgrade` asks uv what it would install without the cooldown and
    explains the second case itself."""

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_held_back_release_is_explained(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        notice = held_back_release_notice("0.27.2")

        assert notice is not None
        assert "ai-quota-swap 0.28.0 is on PyPI" in notice
        assert 'uv-receipt.toml: exclude-newer-span = "P14D"' in notice
        assert 'To take it now:          uv tool upgrade ai-quota-swap --exclude-newer "0 days"' in notice
        assert "To restore the cooldown: uv tool upgrade ai-quota-swap --exclude-newer P14D" in notice
        assert "uv tool install" not in notice
        assert "--exclude-newer" not in self._uv_command(mock_run)

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_absolute_cutoff_is_restored_verbatim(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer = "2026-01-01T00:00:00Z"')
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        notice = held_back_release_notice("0.27.2")

        assert notice is not None
        assert "--exclude-newer 2026-01-01T00:00:00Z" in notice

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_receipt_value_with_shell_characters_gets_no_restore_line(self, mock_run, tmp_path):
        # uv validates what it writes, but the value is pasted into a shell
        # unquoted, so anything outside a duration or timestamp stays out.
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D; echo $HOME"')
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        notice = held_back_release_notice("0.27.2")

        assert notice is not None
        assert "To take it now:" in notice
        assert "To restore the cooldown" not in notice
        assert "echo" not in notice.split("To take it now")[1]

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_env_var_source_is_overridden_by_the_flag(self, mock_run, tmp_path, monkeypatch):
        # The command line flag wins over UV_EXCLUDE_NEWER for that one run,
        # and the variable keeps applying to later upgrades, so there is
        # nothing to unset and nothing to restore.
        monkeypatch.setenv("UV_EXCLUDE_NEWER", "1 week")
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        notice = held_back_release_notice("0.27.2")

        assert notice is not None
        assert "UV_EXCLUDE_NEWER=1 week" in notice
        assert 'uv tool upgrade ai-quota-swap --exclude-newer "0 days"' in notice
        assert "overrides UV_EXCLUDE_NEWER" in notice
        assert "unset" not in notice
        assert "To restore the cooldown" not in notice

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_no_cooldown_means_no_notice_and_no_uv_call(self, mock_run):
        assert held_back_release_notice("0.27.2") is None
        mock_run.assert_not_called()

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_nothing_newer_is_quiet(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = _uv_pip_list([])

        assert held_back_release_notice("0.27.2") is None

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_final_release_is_not_told_about_a_prerelease(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = _uv_pip_list(
            [{"name": "ai-quota-swap", "latest_version": "0.28.0b1"}]
        )

        assert held_back_release_notice("0.27.2") is None

    @patch("ai_quota_swap.update_check.subprocess.run", side_effect=FileNotFoundError)
    def test_uv_failure_is_quiet(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')

        assert held_back_release_notice("0.27.2") is None

    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check._installed_identity_on_disk", return_value=_on_disk("0.28.0"))
    def test_qswap_upgrade_prints_notice_after_uv(
        self, mock_on_disk, mock_run, tmp_path, capsys
    ):
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.side_effect = [MagicMock(returncode=0), _uv_pip_list(_OUTDATED)]

        assert run_self_upgrade("0.27.2") == 0

        out = capsys.readouterr().out
        # The before -> after report is printed ahead of the held-back notice.
        assert "ai-quota-swap upgraded: 0.27.2 -> 0.28.0" in out
        assert "ai-quota-swap 0.28.0 is on PyPI" in out
        assert out.index("upgraded:") < out.index("is on PyPI")
        assert 'uv tool upgrade ai-quota-swap --exclude-newer "0 days"' in out
        upgrade, listing = (c.args[0] for c in mock_run.call_args_list)
        assert upgrade == ["uv", "tool", "upgrade", "ai-quota-swap"]
        assert listing[:4] == ["uv", "pip", "list", "--outdated"]

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_failed_uv_upgrade_gets_no_notice(self, mock_run, tmp_path, capsys):
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = MagicMock(returncode=2)

        assert run_self_upgrade("0.27.2") == 2

        assert "is on PyPI" not in capsys.readouterr().out
        mock_run.assert_called_once()

    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check._installed_identity_on_disk", return_value=None)
    def test_pipx_install_gets_no_notice(
        self, mock_on_disk, mock_run, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr("ai_quota_swap.update_check._detect_install_method", lambda: "pipx")
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = MagicMock(returncode=0)

        assert run_self_upgrade("0.27.2") == 0

        assert "is on PyPI" not in capsys.readouterr().out
        mock_run.assert_called_once()

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_windows_prints_notice_with_the_command(
        self, mock_run, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr("ai_quota_swap.update_check.sys.platform", "win32")
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        assert run_self_upgrade("0.27.2") == 1

        out = capsys.readouterr().out
        assert "uv tool upgrade ai-quota-swap" in out
        assert "is on PyPI" in out
        assert self._uv_command(mock_run)[:4] == ["uv", "pip", "list", "--outdated"]


@patch("ai_quota_swap.update_check.sys.platform", "linux")
class TestRunSelfUpgrade:
    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check._installed_identity_on_disk", return_value=None)
    @patch("ai_quota_swap.update_check._detect_install_method", return_value="uv")
    def test_uv_invokes_uv_tool_upgrade(self, mock_detect, mock_on_disk, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        assert run_self_upgrade() == 0
        mock_run.assert_called_once_with(
            ["uv", "tool", "upgrade", "ai-quota-swap"], check=False
        )

    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check._installed_identity_on_disk", return_value=None)
    @patch("ai_quota_swap.update_check._detect_install_method", return_value="pipx")
    def test_pipx_invokes_pipx_upgrade(self, mock_detect, mock_on_disk, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        assert run_self_upgrade() == 0
        mock_run.assert_called_once_with(
            ["pipx", "upgrade", "ai-quota-swap"], check=False
        )

    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check._detect_install_method", return_value="uv")
    def test_propagates_nonzero_exit_code(self, mock_detect, mock_run):
        mock_run.return_value = MagicMock(returncode=2)

        assert run_self_upgrade() == 2

    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check._detect_install_method", return_value=None)
    def test_unknown_method_returns_1_and_prints_instructions(
        self, mock_detect, mock_run, capsys
    ):
        assert run_self_upgrade() == 1
        mock_run.assert_not_called()
        err = capsys.readouterr().err
        assert "uv tool upgrade ai-quota-swap" in err
        assert "pipx upgrade ai-quota-swap" in err
        assert "-m pip install --upgrade ai-quota-swap\n" in err

    @patch(
        "ai_quota_swap.update_check.subprocess.run", side_effect=FileNotFoundError
    )
    @patch("ai_quota_swap.update_check._detect_install_method", return_value="uv")
    def test_filenotfound_returns_1(self, mock_detect, mock_run, capsys):
        assert run_self_upgrade() == 1
        err = capsys.readouterr().err
        assert "PATH" in err


@patch("ai_quota_swap.update_check.sys.platform", "linux")
class TestRunSelfUpgradeReport:
    """After a successful upgrade, report what actually landed on disk so the
    user is not misled by a package manager that exits 0 without changing
    anything (#491). Verification is best effort: it never masks the
    upgrade's own exit status."""

    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check._detect_install_method", return_value="uv")
    def test_reports_successful_upgrade(self, mock_detect, mock_run, capsys):
        mock_run.return_value = MagicMock(returncode=0)
        with patch(
            "ai_quota_swap.update_check._installed_identity_on_disk", return_value=_on_disk("0.28.0")
        ):
            assert run_self_upgrade("0.27.2") == 0
        assert "ai-quota-swap upgraded: 0.27.2 -> 0.28.0" in capsys.readouterr().out

    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check._detect_install_method", return_value="uv")
    def test_reports_unchanged_upgrade(self, mock_detect, mock_run, capsys):
        mock_run.return_value = MagicMock(returncode=0)
        with patch(
            "ai_quota_swap.update_check._installed_identity_on_disk", return_value=_on_disk("0.27.2")
        ):
            assert run_self_upgrade("0.27.2") == 0
        assert "ai-quota-swap is still at 0.27.2" in capsys.readouterr().out

    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check._detect_install_method", return_value="uv")
    def test_no_result_line_when_verification_fails(self, mock_detect, mock_run, capsys):
        mock_run.return_value = MagicMock(returncode=0)
        with patch(
            "ai_quota_swap.update_check._installed_identity_on_disk", return_value=None
        ):
            assert run_self_upgrade("0.27.2") == 0
        out = capsys.readouterr().out
        assert "upgraded:" not in out
        assert "is still at" not in out

    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check._detect_install_method", return_value="uv")
    def test_no_result_line_when_verification_times_out(self, mock_detect, mock_run, capsys):
        # The verification subprocess.run times out; _installed_identity_on_disk
        # catches subprocess.SubprocessError and returns None, so we stay
        # silent and keep the upgrade's exit status.
        mock_run.side_effect = [
            MagicMock(returncode=0),
            subprocess.TimeoutExpired("py", 10),
            MagicMock(returncode=0, stdout="[]"),
        ]
        assert run_self_upgrade("0.27.2") == 0
        out = capsys.readouterr().out
        assert "upgraded:" not in out
        assert "is still at" not in out

    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check._detect_install_method", return_value="uv")
    def test_failed_upgrade_reports_nothing(self, mock_detect, mock_run, capsys):
        mock_run.return_value = MagicMock(returncode=2)
        assert run_self_upgrade("0.27.2") == 2
        out = capsys.readouterr().out
        assert "upgraded:" not in out
        assert "is still at" not in out


_GIT_AD03D17 = {"version": "0.1.0", "source": "ad03d17" + "a" * 33}
_GIT_FDEB279 = {"version": "0.1.0", "source": "fdeb279" + "b" * 33}


@patch("ai_quota_swap.update_check.sys.platform", "linux")
class TestUpgradeReportNamesTheBuild:
    """Every git install of a release carries that release's version, so the
    report names the commit too; a PyPI install, which has none, reads as it
    did. The package manager and the build on disk are faked."""

    def _upgrade(self, monkeypatch, capsys, before, after):
        from ai_quota_swap import update_check

        monkeypatch.setattr(update_check, "_detect_install_method", lambda: "pipx")
        monkeypatch.setattr(update_check, "install_identity", lambda: dict(before))
        monkeypatch.setattr(update_check, "_installed_identity_on_disk", lambda: after)
        with patch("ai_quota_swap.update_check.subprocess.run",
                   return_value=MagicMock(returncode=0)) as upgrade:
            assert run_self_upgrade(before["version"]) == 0
        upgrade.assert_called_once_with(["pipx", "upgrade", "ai-quota-swap"], check=False)
        return capsys.readouterr().out.splitlines()

    def test_a_new_commit_of_the_same_version_is_an_upgrade(self, monkeypatch, capsys):
        lines = self._upgrade(monkeypatch, capsys, _GIT_AD03D17, dict(_GIT_FDEB279))
        assert lines == ["ai-quota-swap upgraded: 0.1.0 (ad03d17) -> 0.1.0 (fdeb279)"]

    def test_the_same_commit_is_still_at_that_build(self, monkeypatch, capsys):
        lines = self._upgrade(monkeypatch, capsys, _GIT_FDEB279, dict(_GIT_FDEB279))
        assert lines == ["ai-quota-swap is still at 0.1.0 (fdeb279)."]

    def test_a_pypi_install_names_only_the_version(self, monkeypatch, capsys):
        old, new = {"version": "0.1.0", "source": None}, {"version": "0.2.0", "source": None}
        assert self._upgrade(monkeypatch, capsys, old, dict(new)) == [
            "ai-quota-swap upgraded: 0.1.0 -> 0.2.0"
        ]
        assert self._upgrade(monkeypatch, capsys, new, dict(new)) == [
            "ai-quota-swap is still at 0.2.0."
        ]

    def test_a_build_on_disk_that_cannot_be_read_says_nothing(self, monkeypatch, capsys):
        assert self._upgrade(monkeypatch, capsys, _GIT_AD03D17, None) == []


@patch("ai_quota_swap.update_check.sys.platform", "win32")
class TestRunSelfUpgradeWindows:
    """On Windows the running .exe is locked, so we never upgrade in place --
    we print the command for the user to run themselves and exit 1."""

    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check._detect_install_method", return_value="uv")
    def test_uv_prints_command_and_does_not_run(self, mock_detect, mock_run, capsys):
        assert run_self_upgrade() == 1
        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "uv tool upgrade ai-quota-swap" in out

    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check._detect_install_method", return_value="pipx")
    def test_pipx_prints_command_and_does_not_run(self, mock_detect, mock_run, capsys):
        assert run_self_upgrade() == 1
        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "pipx upgrade ai-quota-swap" in out

    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check._detect_install_method", return_value=None)
    def test_unknown_method_hits_generic_fallback(self, mock_detect, mock_run, capsys):
        assert run_self_upgrade() == 1
        mock_run.assert_not_called()
        err = capsys.readouterr().err
        assert "uv tool upgrade ai-quota-swap" in err
        assert "pipx upgrade ai-quota-swap" in err
        assert "-m pip install --upgrade ai-quota-swap\n" in err


# --- build identity + the menu bar restart after an upgrade ------------------


class _Dist:
    def __init__(self, direct_url):
        self._direct_url = direct_url

    def read_text(self, name):
        assert name == "direct_url.json"
        return self._direct_url


class TestInstallIdentity:
    @pytest.fixture(autouse=True)
    def _version_already_read(self):
        # The package looks its version up once, through importlib.metadata,
        # which these tests replace: read it before they do, so each passes
        # when run alone.
        import ai_quota_swap

        assert ai_quota_swap.__version__

    def test_git_install_carries_its_commit(self):
        from ai_quota_swap import update_check

        direct = json.dumps({"url": "https://x/y.git", "vcs_info": {"vcs": "git", "commit_id": "3fec656abc"}})
        with patch("importlib.metadata.distribution", return_value=_Dist(direct)):
            identity = update_check.install_identity()
        assert identity["source"] == "3fec656abc"
        assert identity["version"] == update_check.__version__ if hasattr(update_check, "__version__") else True
        assert update_check.describe_identity(identity).endswith("(3fec656)")

    def test_pypi_install_has_no_source(self):
        from ai_quota_swap import update_check

        with patch("importlib.metadata.distribution", return_value=_Dist(None)):
            identity = update_check.install_identity()
        assert identity["source"] is None
        assert "(" not in update_check.describe_identity(identity)

    def test_unreadable_metadata_is_not_fatal(self):
        from importlib.metadata import PackageNotFoundError

        from ai_quota_swap import update_check

        with patch("importlib.metadata.distribution", side_effect=PackageNotFoundError("ai-quota-swap")):
            assert update_check.install_identity()["source"] is None
        assert update_check.describe_identity(None) == "unknown"


class TestInstalledIdentityOnDisk:
    """After an upgrade, a fresh interpreter reads the build on disk from the
    package metadata alone: the version, and the commit of a git install."""

    def _dist_info(self, root, direct_url):
        info = root / "ai_quota_swap-0.1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: ai-quota-swap\nVersion: 0.1.0\n", encoding="utf-8"
        )
        if direct_url is not None:
            (info / "direct_url.json").write_text(direct_url, encoding="utf-8")

    @pytest.mark.parametrize(
        ("direct_url", "source"),
        [
            (json.dumps({"url": "https://github.com/seangeng/ai-quota-swap.git",
                         "vcs_info": {"vcs": "git", "commit_id": _GIT_FDEB279["source"]}}),
             _GIT_FDEB279["source"]),
            (None, None),  # from PyPI: no direct_url.json
            (json.dumps({"url": "file:///src/ai-quota-swap", "dir_info": {"editable": True}}), None),
            ("not json", None),
        ],
        ids=["git", "pypi", "editable", "unreadable"],
    )
    def test_a_fresh_interpreter_reads_version_and_commit(
        self, direct_url, source, tmp_path, monkeypatch
    ):
        from ai_quota_swap import update_check

        # PYTHONPATH comes before site-packages, so this metadata is the one
        # the child finds.
        self._dist_info(tmp_path, direct_url)
        monkeypatch.setenv("PYTHONPATH", str(tmp_path))
        assert update_check._installed_identity_on_disk() == {"version": "0.1.0", "source": source}

    def test_it_never_imports_the_new_build(self):
        from ai_quota_swap import update_check

        with patch("ai_quota_swap.update_check.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout='{"version": "0.1.0", "source": null}')
            update_check._installed_identity_on_disk()
        command = run.call_args.args[0]
        assert command[:2] == [update_check.sys.executable, "-c"]
        assert "ai_quota_swap" not in command[2]

    @pytest.mark.parametrize(
        "outcome",
        [
            subprocess.CalledProcessError(1, "python"),
            subprocess.TimeoutExpired("python", 10),
            OSError("no interpreter"),
            MagicMock(returncode=0, stdout="Traceback (most recent call last):"),
            MagicMock(returncode=0, stdout='{"source": null}'),
            MagicMock(returncode=0, stdout='["0.1.0"]'),
        ],
        ids=["failed", "timed-out", "not-started", "not-json", "no-version", "not-an-object"],
    )
    def test_any_failure_is_none(self, outcome):
        from ai_quota_swap import update_check

        effect = {"side_effect": outcome} if isinstance(outcome, BaseException) else {
            "return_value": outcome}
        with patch("ai_quota_swap.update_check.subprocess.run", **effect):
            assert update_check._installed_identity_on_disk() is None


_OLD_BUILD = {"version": "0.27.2", "source": None}
_NEW_BUILD = {"version": "0.28.0", "source": None}


@patch("ai_quota_swap.update_check.sys.platform", "darwin")
class TestUpgradeRestartsARunningMenuBar:

    def _launchd(self, monkeypatch, pid):
        calls = []
        monkeypatch.setattr("ai_quota_swap.launch_agent.service_pid", lambda *a, **k: pid)
        monkeypatch.setattr(
            "ai_quota_swap.launch_agent.kickstart", lambda *a, **k: calls.append(k)
        )
        return calls

    def test_restarts_a_running_service_when_the_build_changed(self, monkeypatch, capsys):
        from ai_quota_swap import update_check

        calls = self._launchd(monkeypatch, 4242)
        update_check._restart_menubar_if_rebuilt(_OLD_BUILD, _NEW_BUILD)
        assert calls == [{"restart": True}]
        assert "Menu bar restarted onto 0.28.0 (was pid 4242)" in capsys.readouterr().out

    def test_leaves_a_quit_service_quit(self, monkeypatch):
        # Only a running instance is touched: a deliberate Quit stays quit.
        from ai_quota_swap import update_check

        calls = self._launchd(monkeypatch, None)
        update_check._restart_menubar_if_rebuilt(_OLD_BUILD, _NEW_BUILD)
        assert calls == []

    def test_does_nothing_when_the_build_did_not_change_or_is_unknown(self, monkeypatch):
        from ai_quota_swap import update_check

        calls = self._launchd(monkeypatch, 4242)
        update_check._restart_menubar_if_rebuilt(_OLD_BUILD, _OLD_BUILD)
        update_check._restart_menubar_if_rebuilt(_OLD_BUILD, None)
        assert calls == []

    def test_a_same_version_git_install_still_counts_as_changed(self, monkeypatch):
        from ai_quota_swap import update_check

        calls = self._launchd(monkeypatch, 4242)
        update_check._restart_menubar_if_rebuilt(
            {"version": "1.0.0b1", "source": "aaaaaaa"},
            {"version": "1.0.0b1", "source": "bbbbbbb"},
        )
        assert calls == [{"restart": True}]

    def test_a_failed_restart_is_a_warning_not_a_failed_upgrade(self, monkeypatch, capsys):
        from ai_quota_swap import update_check
        from ai_quota_swap.exceptions import ClaudeSwitchError

        monkeypatch.setattr("ai_quota_swap.launch_agent.service_pid", lambda *a, **k: 4242)

        def boom(*a, **k):
            raise ClaudeSwitchError("launchctl kickstart failed (exit 3)")

        monkeypatch.setattr("ai_quota_swap.launch_agent.kickstart", boom)
        update_check._restart_menubar_if_rebuilt(_OLD_BUILD, _NEW_BUILD)
        captured = capsys.readouterr()
        assert "not restarted" in captured.out + captured.err

    def test_any_provider_error_is_a_warning_too(self, monkeypatch, capsys):
        # The catch is the provider-neutral base, not Claude Code's subclass.
        from ai_quota_swap import update_check
        from ai_quota_swap.exceptions import ClaudeSwitchError, CodexLoginError

        assert not issubclass(CodexLoginError, ClaudeSwitchError)
        monkeypatch.setattr("ai_quota_swap.launch_agent.service_pid", lambda *a, **k: 4242)

        def boom(*a, **k):
            raise CodexLoginError("something only a Codex path raises")

        monkeypatch.setattr("ai_quota_swap.launch_agent.kickstart", boom)
        update_check._restart_menubar_if_rebuilt(_OLD_BUILD, _NEW_BUILD)
        captured = capsys.readouterr()
        assert "not restarted onto the new build: something only a Codex path raises" in (
            captured.out + captured.err
        )

    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check._detect_install_method", return_value="uv")
    def test_run_self_upgrade_wires_the_restart_in(self, mock_detect, mock_run, monkeypatch, capsys):
        from ai_quota_swap import update_check

        mock_run.return_value = MagicMock(returncode=0)
        calls = self._launchd(monkeypatch, 4242)
        monkeypatch.setattr(update_check, "install_identity", lambda: _OLD_BUILD)
        monkeypatch.setattr(update_check, "_installed_identity_on_disk", lambda: _NEW_BUILD)
        assert run_self_upgrade("0.27.2") == 0
        assert calls == [{"restart": True}]
        out = capsys.readouterr().out
        assert "ai-quota-swap upgraded: 0.27.2 -> 0.28.0" in out
        assert "Menu bar restarted" in out


# --- the install commands qswap prints ----------------------------------------


class TestPrintedInstallCommands:
    """Every install command qswap prints names the package on PyPI and is one
    a shell can run as printed: the extra quoted once, for zsh, and nothing
    left over when the shell takes the quotes off."""

    SPEC = "ai-quota-swap[menubar]"

    def _argv(self, command: str) -> list[str]:
        import shlex

        argv = shlex.split(command)
        assert not any(quote in arg for arg in argv for quote in "'\""), argv
        assert not any("git+" in arg or "@" in arg for arg in argv), argv
        return argv

    def test_the_constants_name_the_released_package(self, monkeypatch):
        from ai_quota_swap import update_check

        monkeypatch.setattr("ai_quota_swap.update_check.sys.prefix", "/home/me/venv")
        assert update_check.INSTALL_SOURCE == "ai-quota-swap"
        assert update_check.menubar_install_spec() == f"'{self.SPEC}'"

    @pytest.mark.parametrize(
        ("method", "command"),
        [
            ("uv", ["uv", "tool", "install", "--force", SPEC]),
            ("pipx", ["pipx", "install", "--force", SPEC]),
            (None, ["pip", "install", SPEC]),
        ],
        ids=["uv", "pipx", "unknown"],
    )
    def test_the_menubar_extra_remedy(self, monkeypatch, method, command):
        import importlib.util

        from ai_quota_swap import cli

        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
        monkeypatch.setattr("ai_quota_swap.update_check._detect_install_method", lambda: method)
        _, remedy = cli._menubar_extra_missing().split("Install it with: ")
        assert self._argv(remedy) == command

    @pytest.mark.parametrize(
        ("method", "command"),
        [
            ("uv", ["uv", "tool", "install", "--managed-python", "--force", SPEC]),
            ("pipx", ["pipx", "install", "--force", "--python", "<that", "python>", SPEC]),
        ],
        ids=["uv", "pipx"],
    )
    def test_the_framework_build_remedy(self, method, command):
        from ai_quota_swap import menubar

        text = menubar.framework_build_warning(
            framework="Python", install_method=method, mac_ver="26.1"
        )
        last = text.splitlines()[-1]
        assert self._argv(last) == command

    @patch("ai_quota_swap.update_check.subprocess.run")
    @patch("ai_quota_swap.update_check._detect_install_method", return_value=None)
    def test_the_upgrade_fallback(self, mock_detect, mock_run, monkeypatch, capsys):
        import shlex

        # A space in the interpreter's path must not split it in two. Either
        # quoting (POSIX, or cmd.exe's on Windows) reads back the same here.
        python = "/opt/my tools/venv/bin/python"
        monkeypatch.setattr("ai_quota_swap.update_check.sys.executable", python)
        assert run_self_upgrade() == 1
        lines = [line.strip() for line in capsys.readouterr().err.splitlines()]
        pip = next(line for line in lines if " -m pip " in line)
        assert shlex.split(pip) == [python, "-m", "pip", "install", "--upgrade", "ai-quota-swap"]

    @pytest.mark.parametrize(
        ("platform", "path", "word"),
        [
            ("darwin", "/opt/my tools/venv/bin/python", "'/opt/my tools/venv/bin/python'"),
            ("linux", "/usr/bin/python3", "/usr/bin/python3"),
            ("win32", r"C:\Users\Maya Lee\venv\Scripts\python.exe",
             r'"C:\Users\Maya Lee\venv\Scripts\python.exe"'),
            ("win32", r"C:\Python312\python.exe", r"C:\Python312\python.exe"),
        ],
        ids=["posix-space", "posix-plain", "windows-space", "windows-plain"],
    )
    def test_the_interpreter_is_one_shell_word(self, monkeypatch, platform, path, word):
        from ai_quota_swap import update_check

        monkeypatch.setattr("ai_quota_swap.update_check.sys.platform", platform)
        assert update_check._shell_word(path) == word


# --- the install's own name: ai-quota-swap, or the qswap alias ----------------


_UV_TOOL = "/home/maya/.local/share/uv/tools/"
_PIPX_VENV = "/home/maya/.local/pipx/venvs/"


class TestInstallName:
    """`uv tool install qswap` makes a uv tool named qswap (packaging/qswap
    depends on ai-quota-swap). Every command qswap runs or prints names the
    tool by the last folder of sys.prefix; an install that is not a uv tool or
    a pipx venv keeps the package's own name."""

    @pytest.fixture(autouse=True)
    def _install(self, monkeypatch, tmp_path):
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("PIPX_HOME", raising=False)
        monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
        monkeypatch.setattr("ai_quota_swap.update_check.sys.platform", "linux")
        monkeypatch.setattr("ai_quota_swap.update_check.CACHE_PATH", tmp_path / "cache.json")

    def _prefix(self, monkeypatch, prefix: str) -> None:
        monkeypatch.setattr("ai_quota_swap.update_check.sys.prefix", prefix)

    def _upgrade(self, monkeypatch, before="0.1.0", after="0.1.1"):
        """run_self_upgrade with the package manager and the build on disk faked."""
        from ai_quota_swap import update_check

        monkeypatch.setattr(
            update_check, "install_identity", lambda: {"version": before, "source": None}
        )
        monkeypatch.setattr(update_check, "_installed_identity_on_disk", lambda: _on_disk(after))
        with patch("ai_quota_swap.update_check.subprocess.run",
                   return_value=MagicMock(returncode=0)) as run:
            code = run_self_upgrade(before)
        return code, run

    @pytest.mark.parametrize(
        ("prefix", "name"),
        [
            (_UV_TOOL + "qswap", "qswap"),
            (_UV_TOOL + "ai-quota-swap", "ai-quota-swap"),
            (_PIPX_VENV + "qswap", "qswap"),
            (_PIPX_VENV + "ai-quota-swap", "ai-quota-swap"),
            ("/home/maya/code/ai-quota-swap/.venv", "ai-quota-swap"),
            ("/home/maya/venv", "ai-quota-swap"),
            # Folders that are neither name: never pasted into a command.
            (_PIPX_VENV + "ai-quota-swap_x", "ai-quota-swap"),
            ("/home/maya/src/uv/tools/proj/venv", "ai-quota-swap"),
            ("/opt/t/work/envs/thing", "ai-quota-swap"),
        ],
        ids=["uv-alias", "uv", "pipx-alias", "pipx", "checkout", "plain-venv",
             "pipx-suffix", "nested-under-uv-tools", "under-UV_TOOL_DIR"],
    )
    def test_the_name_comes_from_the_install(self, monkeypatch, prefix, name):
        from ai_quota_swap import update_check

        self._prefix(monkeypatch, prefix)
        if prefix.startswith("/opt/t/"):
            monkeypatch.setenv("UV_TOOL_DIR", "/opt/t")
            assert update_check._detect_install_method() == "uv"
        assert update_check.install_name() == name
        assert update_check.menubar_install_spec() == f"'{name}[menubar]'"

    @pytest.mark.parametrize(
        ("prefix", "tool_dir"),
        [
            (r"C:\Users\maya\.local\share\uv\tools\qswap", None),
            (r"D:\tools\qswap", r"D:\tools"),
        ],
        ids=["uv-tools-path", "UV_TOOL_DIR"],
    )
    def test_a_windows_path(self, monkeypatch, capsys, prefix, tool_dir):
        from ai_quota_swap import update_check

        monkeypatch.setattr("ai_quota_swap.update_check.sys.platform", "win32")
        self._prefix(monkeypatch, prefix)
        if tool_dir:
            monkeypatch.setenv("UV_TOOL_DIR", tool_dir)
        assert update_check._detect_install_method() == "uv"
        assert update_check.install_name() == "qswap"

        code, run = self._upgrade(monkeypatch)
        assert code == 1
        run.assert_not_called()
        assert capsys.readouterr().out == (
            "To upgrade ai-quota-swap on Windows, run:\n  uv tool upgrade qswap\n"
        )

    def test_a_uv_alias_upgrades_the_tool_it_is(self, monkeypatch, capsys):
        self._prefix(monkeypatch, _UV_TOOL + "qswap")
        code, run = self._upgrade(monkeypatch)
        assert code == 0
        run.assert_called_once_with(["uv", "tool", "upgrade", "qswap"], check=False)
        # The versions are ai-quota-swap's, never the alias's.
        assert capsys.readouterr().out == "ai-quota-swap upgraded: 0.1.0 -> 0.1.1\n"

    def test_an_ai_quota_swap_uv_tool_is_unchanged(self, monkeypatch, capsys):
        self._prefix(monkeypatch, _UV_TOOL + "ai-quota-swap")
        code, run = self._upgrade(monkeypatch)
        assert code == 0
        run.assert_called_once_with(["uv", "tool", "upgrade", "ai-quota-swap"], check=False)
        assert capsys.readouterr().out == "ai-quota-swap upgraded: 0.1.0 -> 0.1.1\n"

    def test_a_git_install_named_ai_quota_swap_is_unchanged(self, monkeypatch, capsys):
        from ai_quota_swap import update_check

        self._prefix(monkeypatch, _UV_TOOL + "ai-quota-swap")
        monkeypatch.setattr(update_check, "install_identity", lambda: dict(_GIT_AD03D17))
        monkeypatch.setattr(
            update_check, "_installed_identity_on_disk", lambda: dict(_GIT_FDEB279)
        )
        with patch("ai_quota_swap.update_check.subprocess.run",
                   return_value=MagicMock(returncode=0)) as run:
            assert run_self_upgrade("0.1.0") == 0
        run.assert_called_once_with(["uv", "tool", "upgrade", "ai-quota-swap"], check=False)
        assert capsys.readouterr().out == (
            "ai-quota-swap upgraded: 0.1.0 (ad03d17) -> 0.1.0 (fdeb279)\n"
        )

    @pytest.mark.parametrize(
        ("folder", "command"),
        [
            ("ai-quota-swap", ["pipx", "upgrade", "ai-quota-swap"]),
            # pipx upgrade qswap would upgrade only the alias, which stays at
            # 0.1.0: runpip upgrades ai-quota-swap inside the qswap venv.
            ("qswap", ["pipx", "runpip", "qswap", "install", "--upgrade", "ai-quota-swap"]),
        ],
        ids=["pipx", "pipx-alias"],
    )
    def test_pipx_under_either_name(self, monkeypatch, capsys, folder, command):
        self._prefix(monkeypatch, _PIPX_VENV + folder)
        code, run = self._upgrade(monkeypatch)
        assert code == 0
        run.assert_called_once_with(command, check=False)
        assert capsys.readouterr().out == "ai-quota-swap upgraded: 0.1.0 -> 0.1.1\n"

    @pytest.mark.parametrize(
        ("prefix", "direct"),
        [
            (_UV_TOOL + "qswap", "uv tool upgrade qswap"),
            (_UV_TOOL + "ai-quota-swap", "uv tool upgrade ai-quota-swap"),
            (_PIPX_VENV + "qswap", "pipx runpip qswap install --upgrade ai-quota-swap"),
            (_PIPX_VENV + "ai-quota-swap", "pipx upgrade ai-quota-swap"),
        ],
        ids=["uv-alias", "uv", "pipx-alias", "pipx"],
    )
    @patch("ai_quota_swap.update_check.urllib.request.urlopen")
    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_the_windows_update_notice(self, mock_run, mock_urlopen, monkeypatch, prefix, direct):
        monkeypatch.setattr("ai_quota_swap.update_check.sys.platform", "win32")
        self._prefix(monkeypatch, prefix.replace("/", "\\"))
        mock_run.return_value = _uv_pip_list(
            [{"name": "ai-quota-swap", "version": "0.1.0", "latest_version": "0.1.1"}]
        )
        mock_urlopen.return_value = _make_pypi_response("0.1.1")
        assert check_for_update("0.1.0") == (
            "A newer version of ai-quota-swap is available (0.1.1). "
            f"You are using 0.1.0. Run `{direct}` to update."
        )

    @patch("ai_quota_swap.update_check.subprocess.run")
    def test_the_cooldown_remedy_names_the_alias(self, mock_run, monkeypatch, tmp_path):
        prefix = tmp_path / "uv" / "tools" / "qswap"
        prefix.mkdir(parents=True)
        _UvInstall._write_receipt(prefix, 'exclude-newer-span = "P14D"', '{ name = "qswap" }')
        self._prefix(monkeypatch, str(prefix))
        mock_run.return_value = _uv_pip_list(
            [{"name": "ai-quota-swap", "version": "0.1.0", "latest_version": "0.1.1"}]
        )
        lines = held_back_release_notice("0.1.0").splitlines()
        assert lines[0].startswith("ai-quota-swap 0.1.1 is on PyPI")
        assert lines[2:] == [
            '  To take it now:          uv tool upgrade qswap --exclude-newer "0 days"',
            "  To restore the cooldown: uv tool upgrade qswap --exclude-newer P14D",
        ]

    def test_a_checkout_prints_todays_instructions(self, monkeypatch, capsys):
        python = "/home/maya/code/ai-quota-swap/.venv/bin/python"
        self._prefix(monkeypatch, "/home/maya/code/ai-quota-swap/.venv")
        monkeypatch.setattr("ai_quota_swap.update_check.sys.executable", python)
        code, run = self._upgrade(monkeypatch)
        assert code == 1
        run.assert_not_called()
        assert capsys.readouterr().err.splitlines()[-5:] == [
            "Upgrade it the way you installed it:",
            "  uv tool upgrade ai-quota-swap",
            "  pipx upgrade ai-quota-swap",
            f"  {python} -m pip install --upgrade ai-quota-swap",
            "  git pull                      # a source checkout",
        ]

    @pytest.mark.parametrize(
        ("folder", "remedy"),
        [
            (_UV_TOOL + "qswap", "uv tool install --force 'qswap[menubar]'"),
            (_UV_TOOL + "ai-quota-swap", "uv tool install --force 'ai-quota-swap[menubar]'"),
            (_PIPX_VENV + "qswap", "pipx install --force 'qswap[menubar]'"),
            (_PIPX_VENV + "ai-quota-swap", "pipx install --force 'ai-quota-swap[menubar]'"),
            ("/home/maya/venv", "pip install 'ai-quota-swap[menubar]'"),
        ],
        ids=["uv-alias", "uv", "pipx-alias", "pipx", "plain-venv"],
    )
    def test_the_menubar_extra_remedy_reinstalls_the_same_tool(
        self, monkeypatch, folder, remedy
    ):
        import importlib.util

        from ai_quota_swap import cli

        self._prefix(monkeypatch, folder)
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
        assert cli._menubar_extra_missing() == (
            f"The menu bar needs the 'menubar' extra. Install it with: {remedy}"
        )

    @pytest.mark.parametrize(
        ("folder", "remedy"),
        [
            (_UV_TOOL + "qswap", "  uv tool install --managed-python --force 'qswap[menubar]'"),
            (_PIPX_VENV + "qswap",
             "  pipx install --force --python <that python> 'qswap[menubar]'"),
        ],
        ids=["uv-alias", "pipx-alias"],
    )
    def test_the_framework_build_remedy_names_the_alias(self, monkeypatch, folder, remedy):
        from ai_quota_swap import menubar

        self._prefix(monkeypatch, folder)
        text = menubar.framework_build_warning(framework="Python", mac_ver="26.1")
        assert text.splitlines()[-1] == remedy

    def test_the_build_on_disk_is_ai_quota_swaps(self, tmp_path, monkeypatch):
        """An alias venv holds both packages; the after-version is ai-quota-swap's."""
        from ai_quota_swap import update_check

        for name, dist, version in (("qswap", "qswap", "0.1.0"),
                                    ("ai-quota-swap", "ai_quota_swap", "0.1.1")):
            info = tmp_path / f"{dist}-{version}.dist-info"
            info.mkdir()
            (info / "METADATA").write_text(
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
            )
        monkeypatch.setenv("PYTHONPATH", str(tmp_path))
        assert update_check._installed_identity_on_disk() == {"version": "0.1.1", "source": None}
