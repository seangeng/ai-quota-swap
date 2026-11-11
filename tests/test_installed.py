"""The command that runs this install of qswap (decision B2 of the PR #13
round 2 review): the console script found from the package's own
environment, preferring the PATH-visible link to the same script; another
install's ``qswap`` first on PATH is never used; and only when this install
has no console script, the interpreter with ``-P -m ai_quota_swap``.

Every branch runs the real resolver on a fake layout in ``tmp_path``: a uv
tool virtualenv, the menu bar's bundle, an editable checkout, a ``--user``
install and a Windows one. Nothing here goes through the fixture of
tests/test_key_helper_live.py that fixes the command.
"""

from __future__ import annotations

import json
import os
import sys
import sysconfig
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_quota_swap import installed, launch_agent

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="symbolic links and sh")


def _script(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\necho "ran $*"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


def _install(site: Path, scripts: list[str], *, version: str = "0.1.0") -> None:
    """A distribution record in ``site`` naming ``scripts`` (relative to it)
    and the package beside it, as uv and pip write it."""
    info = site / f"ai_quota_swap-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    rows = [f"{s},sha256=x,1" for s in scripts]
    rows += ["ai_quota_swap/__init__.py,sha256=x,1", f"{info.name}/RECORD,,"]
    (info / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (site / "ai_quota_swap").mkdir(exist_ok=True)
    (site / "ai_quota_swap" / "__init__.py").write_text("", encoding="utf-8")


def _editable(site: Path, checkout: Path, scripts: list[str]) -> None:
    """The record an editable install writes (uv, pip): the scripts, a
    ``.pth`` file and no package of its own, with ``direct_url.json`` naming
    the checkout."""
    info = site / "ai_quota_swap-0.1.0.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    rows = [f"{s},sha256=x,1" for s in scripts]
    rows += ["_editable_impl_ai_quota_swap.pth,sha256=x,42", f"{info.name}/RECORD,,",
             f"{info.name}/direct_url.json,sha256=x,84"]
    (info / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (info / "direct_url.json").write_text(json.dumps({
        "url": checkout.resolve().as_uri(), "dir_info": {"editable": True},
    }), encoding="utf-8")


def _tool_venv(root: Path) -> tuple[Path, Path]:
    """A uv tool virtualenv: ``(site-packages, bin/qswap)``."""
    (root / "pyvenv.cfg").parent.mkdir(parents=True, exist_ok=True)
    (root / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    site = root / "lib" / "python3.12" / "site-packages"
    _install(site, ["../../../bin/qswap", "../../../bin/ai-quota-swap"])
    script = _script(root / "bin" / "qswap")
    _script(root / "bin" / "ai-quota-swap")
    return site, script


def _link(link: Path, target: Path) -> Path:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    return link


def _resolve(site: Path, *, path: str = os.defpath, argv0: str = "/x/pytest",
             site_dirs: list[Path] | None = None) -> list[str]:
    return installed.command(
        package_dir=site / "ai_quota_swap", site_dirs=site_dirs or [],
        path=path, argv0=argv0, executable="/usr/bin/python3",
    )


@POSIX_ONLY
class TestTheToolVirtualenv:
    def test_the_path_link_to_this_install_is_used_unresolved(self, tmp_path):
        site, script = _tool_venv(tmp_path / "tools" / "ai-quota-swap")
        link = _link(tmp_path / "local" / "bin" / "qswap", script)
        assert _resolve(site, path=str(link.parent)) == [str(link)]

    def test_without_a_path_link_the_script_itself(self, tmp_path):
        site, script = _tool_venv(tmp_path / "tools" / "ai-quota-swap")
        assert _resolve(site) == [str(script)]

    def test_another_installs_qswap_first_on_path_is_not_used(self, tmp_path):
        site, script = _tool_venv(tmp_path / "tools" / "ai-quota-swap")
        other = _script(tmp_path / "homebrew" / "bin" / "qswap")
        link = _link(tmp_path / "local" / "bin" / "qswap", script)
        path = os.pathsep.join([str(other.parent), str(link.parent)])
        assert _resolve(site, path=path) == [str(script)]

    def test_an_argv0_link_to_this_install_is_kept_and_made_absolute(
        self, tmp_path, monkeypatch
    ):
        site, script = _tool_venv(tmp_path / "tools" / "ai-quota-swap")
        link = _link(tmp_path / "local" / "bin" / "qswap", script)
        assert _resolve(site, argv0=str(link)) == [str(link)]
        monkeypatch.chdir(link.parent.parent)
        result = _resolve(site, argv0="./bin/qswap")
        assert result == [str(link)] and Path(result[0]).is_absolute()

    def test_an_argv0_of_another_install_is_not_used(self, tmp_path):
        site, script = _tool_venv(tmp_path / "tools" / "ai-quota-swap")
        other = _script(tmp_path / "elsewhere" / "qswap")
        assert _resolve(site, argv0=str(other)) == [str(script)]

    def test_qswap_before_ai_quota_swap(self, tmp_path):
        site, script = _tool_venv(tmp_path / "tools" / "ai-quota-swap")
        assert installed.own_scripts(package_dir=site / "ai_quota_swap", site_dirs=[]) == [
            script, script.parent / "ai-quota-swap",
        ]


@POSIX_ONLY
class TestTheMenuBarBundle:
    """The menu bar runs the bundle's interpreter, whose ``Contents/lib``
    links into the install's own environment, with the plist's PATH."""

    def _bundle(self, tmp_path) -> tuple[Path, Path, Path]:
        venv = tmp_path / "tools" / "ai-quota-swap"
        site, script = _tool_venv(venv)
        contents = tmp_path / "Applications" / "AI Quota Swap Menubar.app" / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        _link(contents / "lib" / "python3.12", venv / "lib" / "python3.12")
        return contents, script, contents / "lib" / "python3.12" / "site-packages"

    def test_the_script_is_found_without_path(self, tmp_path):
        contents, script, bundle_site = self._bundle(tmp_path)
        result = installed.command(
            package_dir=bundle_site / "ai_quota_swap", site_dirs=[bundle_site],
            path="/usr/bin:/bin:/usr/sbin:/sbin",
            argv0=str(bundle_site / "ai_quota_swap" / "__main__.py"),
            executable=str(contents / "MacOS" / "python"),
        )
        assert result == [str(script)]

    def test_the_plists_path_gives_the_link(self, tmp_path):
        contents, script, bundle_site = self._bundle(tmp_path)
        link = _link(tmp_path / "home" / ".local" / "bin" / "qswap", script)
        result = installed.command(
            package_dir=bundle_site / "ai_quota_swap", site_dirs=[bundle_site],
            path=f"{link.parent}:/usr/bin:/bin",
            argv0=str(bundle_site / "ai_quota_swap" / "__main__.py"),
            executable=str(contents / "MacOS" / "python"),
        )
        assert result == [str(link)]


@POSIX_ONLY
class TestOtherInstalls:
    def test_an_editable_checkout_finds_its_environments_script(self, tmp_path):
        """The package is in the checkout's src/, the record in .venv (the
        record an editable install writes: no package of its own)."""
        checkout = tmp_path / "checkout"
        package = checkout / "src" / "ai_quota_swap"
        package.mkdir(parents=True)
        site = checkout / ".venv" / "lib" / "python3.12" / "site-packages"
        _editable(site, checkout, ["../../../bin/qswap"])
        script = _script(checkout / ".venv" / "bin" / "qswap")
        result = installed.command(
            package_dir=package, site_dirs=[site], path=os.defpath, argv0="/x/pytest",
            executable=str(checkout / ".venv" / "bin" / "python"),
        )
        assert result == [str(script)]

    def test_another_install_in_the_interpreters_site_is_not_this_one(self, tmp_path):
        """The re-check's case: PYTHONPATH=src with the python of another
        install. That install's record (with its own package copy) named its
        console script, which may be older and not know key-helper."""
        checkout = tmp_path / "checkout"
        package = checkout / "src" / "ai_quota_swap"
        package.mkdir(parents=True)
        other = tmp_path / "other" / "lib" / "python3.12" / "site-packages"
        _install(other, ["../../../bin/qswap"])
        _script(tmp_path / "other" / "bin" / "qswap")
        result = installed.command(
            package_dir=package, site_dirs=[other], path=os.defpath, argv0="/x/pytest",
            executable="/usr/bin/python3",
        )
        assert result == ["/usr/bin/python3", "-P", "-m", "ai_quota_swap"]

    def test_an_editable_install_of_another_checkout_is_not_this_one(self, tmp_path):
        checkout = tmp_path / "checkout"
        package = checkout / "src" / "ai_quota_swap"
        package.mkdir(parents=True)
        elsewhere = tmp_path / "worktree-b"
        site = elsewhere / ".venv" / "lib" / "python3.12" / "site-packages"
        _editable(site, elsewhere, ["../../../bin/qswap"])
        _script(elsewhere / ".venv" / "bin" / "qswap")
        result = installed.command(
            package_dir=package, site_dirs=[site], path=os.defpath, argv0="/x/pytest",
            executable="/usr/bin/python3",
        )
        assert result == ["/usr/bin/python3", "-P", "-m", "ai_quota_swap"]

    def test_a_user_install(self, tmp_path):
        site = tmp_path / "home" / ".local" / "lib" / "python3.12" / "site-packages"
        _install(site, ["../../../bin/qswap"])
        script = _script(tmp_path / "home" / ".local" / "bin" / "qswap")
        assert _resolve(site) == [str(script)]

    def test_a_windows_install(self, tmp_path):
        site = tmp_path / "venv" / "Lib" / "site-packages"
        _install(site, ["../../Scripts/qswap.exe", "../../Scripts/ai-quota-swap.exe"])
        script = _script(tmp_path / "venv" / "Scripts" / "qswap.exe")
        assert _resolve(site) == [str(script)]

    def test_a_record_with_a_quoted_path(self, tmp_path):
        site = tmp_path / "a,b" / "lib" / "python3.12" / "site-packages"
        info = site / "ai_quota_swap-0.1.0.dist-info"
        info.mkdir(parents=True)
        (info / "RECORD").write_text('"../../../bin/qswap",sha256=x,1\n', encoding="utf-8")
        script = _script(tmp_path / "a,b" / "bin" / "qswap")
        assert _resolve(site) == [str(script)]


@POSIX_ONLY
class TestNoConsoleScript:
    def test_the_interpreter_with_dash_p(self, tmp_path):
        """``-P`` keeps the session's project directory off sys.path: the
        bare ``-m`` form imports project files named like standard-library
        modules (F17)."""
        site = tmp_path / "lib" / "python3.12" / "site-packages"
        _install(site, [])
        assert _resolve(site) == ["/usr/bin/python3", "-P", "-m", "ai_quota_swap"]

    def test_a_qswap_on_path_is_not_taken_for_this_install(self, tmp_path):
        site = tmp_path / "lib" / "python3.12" / "site-packages"
        _install(site, ["../../../bin/qswap"])  # recorded, but deleted since
        other = _script(tmp_path / "other" / "bin" / "qswap")
        assert _resolve(site, path=str(other.parent), argv0=str(other)) == [
            "/usr/bin/python3", "-P", "-m", "ai_quota_swap",
        ]

    def test_no_record_at_all(self, tmp_path):
        package = tmp_path / "somewhere" / "ai_quota_swap"
        package.mkdir(parents=True)
        result = installed.command(
            package_dir=package, site_dirs=[tmp_path / "nothing"], path=os.defpath,
            argv0="", executable="/usr/bin/python3",
        )
        assert result == ["/usr/bin/python3", "-P", "-m", "ai_quota_swap"]


class TestThisProcess:
    def test_the_test_environment_resolves_to_its_own_script(self):
        """With nothing passed in, the real inputs: the running environment
        is an install of this checkout with a console script."""
        expected = Path(sysconfig.get_path("scripts")) / (
            "qswap.exe" if sys.platform == "win32" else "qswap"
        )
        if not expected.is_file():
            pytest.skip("this environment has no qswap console script")
        result = installed.command()
        assert len(result) == 1
        assert os.path.samefile(result[0], expected)

    def test_launch_agent_uses_the_same_resolver(self, monkeypatch):
        monkeypatch.setattr(installed, "command", lambda: ["/opt/qswap/bin/qswap"])
        assert launch_agent.resolve_program() == ["/opt/qswap/bin/qswap"]

    def test_the_line_uses_the_same_resolver(self, monkeypatch):
        from ai_quota_swap import claude_settings

        monkeypatch.setattr(installed, "command", lambda: ["/opt/qswap/bin/qswap"])
        with patch.object(sys, "platform", "darwin"):
            assert claude_settings.helper_command(5) == "/opt/qswap/bin/qswap key-helper 5"
