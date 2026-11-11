"""The suite never reaches the real launchd (tests/conftest.py, "launchd").

A fake HOME protects files, not launchd: launchd keeps one set of services per
user, so a `launchctl bootout` from a test stops the developer's real menu bar.
A throwaway test that ran `qswap purge --yes` in the fake home did exactly
that. These prove the guard: with nothing faked, the paths that drove
launchctl then answer from the guard, a call that would change launchd's state
raises, and no process named launchctl is ever started.

Every test here spies on ``subprocess.run`` and ``subprocess.Popen`` with
stand-ins that record the argv and start nothing, so even a broken guard
cannot reach the real launchctl from this file.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_quota_swap import cli, doctor, launch_agent
from tests import conftest
from tests.conftest import LAUNCHCTL_READ_ONLY, LaunchctlBlocked

POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="launchd's targets are named by the POSIX uid (os.getuid)",
)


def _argv(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [os.fsdecode(v) for v in value]
    return [os.fsdecode(value)]


@pytest.fixture
def spawned(monkeypatch):
    """Every argv handed to ``subprocess.run`` or ``subprocess.Popen``.

    Neither starts anything: ``run`` answers as a failed command and ``Popen``
    raises as for a missing program.
    """
    seen: list[list[str]] = []

    def run(argv, *args, **kwargs):
        seen.append(_argv(argv))
        text = kwargs.get("text") or kwargs.get("universal_newlines")
        return subprocess.CompletedProcess(argv, 1, "" if text else b"", "" if text else b"")

    class Popen:
        def __init__(self, argv, *args, **kwargs):
            seen.append(_argv(argv))
            raise OSError(errno.ENOENT, "the test's spy starts nothing")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", Popen)
    return seen


def _launchctl_runs(seen: list[list[str]]) -> list[list[str]]:
    return [argv for argv in seen if argv and os.path.basename(argv[0]) == "launchctl"]


@pytest.fixture
def on_macos(monkeypatch):
    """The darwin path, on every runner: launch_agent and purge refuse or skip
    the service anywhere else."""
    monkeypatch.setattr(sys, "platform", "darwin")


def _main(argv: list[str]) -> int:
    with patch.object(sys, "argv", ["qswap", *argv]):
        try:
            cli.main()
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr("ai_quota_swap.update_check.check_for_update", lambda v: None)


# -- launch_agent ---------------------------------------------------------------


@POSIX_ONLY
def test_uninstall_with_nothing_faked_asks_only_the_guard(on_macos, spawned, launchctl_guard):
    """The incident's path: is the service loaded, then boot it out. The guard
    answers "not loaded", so there is nothing to boot out, and launchctl is
    never started."""
    result = launch_agent.uninstall()

    assert result["was_loaded"] is False
    assert _launchctl_runs(spawned) == [] and spawned == []
    assert [call[0] for call in launchctl_guard.answered] == ["print", "print-disabled"]


@POSIX_ONLY
def test_uninstall_cannot_boot_out_when_a_test_fakes_only_half_of_launchd(
    on_macos, spawned, launchctl_guard, monkeypatch
):
    """A test that says the service is loaded and fakes nothing else reaches
    `launchctl bootout`: the guard raises, naming the verb."""
    monkeypatch.setattr(launch_agent, "is_loaded", lambda *a, **k: True)

    with pytest.raises(AssertionError, match=r"reached launchctl bootout unfaked"):
        launch_agent.uninstall()

    assert spawned == []
    refused = launchctl_guard.drain()
    assert len(refused) == 1 and "launchctl bootout gui/" in refused[0][0]


@POSIX_ONLY
def test_purge_yes_with_nothing_faked_starts_no_launchctl(
    temp_home, on_macos, spawned, launchctl_guard, quiet
):
    assert _main(["purge", "--yes"]) == 0

    assert _launchctl_runs(spawned) == []
    assert ("print",) == launchctl_guard.answered[0][:1]
    assert not launchctl_guard.refused


@POSIX_ONLY
def test_purge_yes_reports_the_guard_rather_than_booting_out(
    temp_home, on_macos, spawned, launchctl_guard, quiet, monkeypatch
):
    monkeypatch.setattr(launch_agent, "is_loaded", lambda *a, **k: True)

    with pytest.raises(AssertionError, match=r"reached launchctl bootout unfaked"):
        _main(["purge", "--yes"])

    assert _launchctl_runs(spawned) == []
    assert [m for m, _ in launchctl_guard.drain()] and not launchctl_guard.refused


@pytest.mark.parametrize(
    "verb",
    ["bootout", "bootstrap", "kickstart", "enable", "disable", "remove", "load",
     "unload", "submit", "kill", "setenv", "a-verb-the-guard-does-not-know"],
)
def test_every_call_that_is_not_known_to_be_read_only_raises(verb, spawned, launchctl_guard):
    with pytest.raises(AssertionError) as raised:
        launch_agent._launchctl(verb, "gui/501/com.aiquotaswap.menubar")

    assert f"reached launchctl {verb} unfaked" in str(raised.value)
    assert isinstance(raised.value, LaunchctlBlocked)
    assert not isinstance(raised.value, OSError)
    assert spawned == []
    assert len(launchctl_guard.drain()) == 1


@pytest.mark.parametrize("verb", sorted(LAUNCHCTL_READ_ONLY))
def test_a_read_only_call_says_not_loaded_and_runs_nothing(verb, spawned, launchctl_guard):
    answer = launch_agent._launchctl(verb, "gui/501/com.aiquotaswap.menubar")

    assert answer.returncode != 0 and answer.stdout == ""
    assert "not loaded" in answer.stderr
    assert spawned == []
    assert launchctl_guard.answered == [(verb, "gui/501/com.aiquotaswap.menubar")]


def test_the_read_only_answers_read_as_not_loaded_to_launch_agent(spawned, launchctl_guard):
    assert launch_agent.is_loaded(uid=501) is False
    assert launch_agent._launchd_disabled(uid=501) is False
    assert launch_agent.service_pid(uid=501) is None
    assert spawned == []


def test_a_test_that_fakes_launchctl_itself_sits_on_top_of_the_guard(monkeypatch, spawned):
    calls = []

    def fake(*args):
        calls.append(args)
        return subprocess.CompletedProcess(["launchctl", *args], 0, "\tpid = 4242\n", "")

    monkeypatch.setattr(launch_agent, "_launchctl", fake)

    assert launch_agent.service_pid(uid=501) == 4242
    assert calls == [("print", "gui/501/com.aiquotaswap.menubar")]
    assert spawned == []


# -- doctor ---------------------------------------------------------------------


def test_doctor_reads_launchctl_print_as_not_loaded(spawned, launchctl_guard):
    code, text = doctor.default_probes().launchctl_print()

    assert code != 0 and text == ""
    assert spawned == []
    assert launchctl_guard.answered == [("print",)]


# -- the audit hook underneath --------------------------------------------------
#
# Raised as synthetic audit events, so a broken hook cannot start anything.


@pytest.mark.parametrize(
    ("event", "args"),
    [
        ("subprocess.Popen", ("launchctl", ["launchctl", "bootout", "gui/501/x"], None, None)),
        ("subprocess.Popen", ("/bin/launchctl", ["/bin/launchctl", "print", "gui/501/x"],
                              None, None)),
        ("subprocess.Popen", ("/bin/sh", ["/bin/sh", "-c", "launchctl bootout gui/501/x"],
                              None, None)),
        ("subprocess.Popen", (b"/bin/launchctl", [b"/bin/launchctl", b"list"], None, None)),
        ("os.posix_spawn", ("/bin/launchctl", ["launchctl", "unload", "x.plist"], {})),
        ("os.exec", ("/bin/launchctl", ["launchctl", "remove", "x"], {})),
        ("os.spawn", (0, "/bin/launchctl", ["launchctl", "kickstart", "-k", "x"], {})),
        ("os.system", ("cd /tmp && /bin/launchctl bootout gui/501/x",)),
        ("pty.spawn", (["launchctl", "print", "x"],)),
    ],
)
def test_the_audit_hook_refuses_to_start_launchctl_by_any_route(event, args, launchctl_guard):
    with pytest.raises(LaunchctlBlocked, match="started launchctl"):
        sys.audit(event, *args)

    assert len(launchctl_guard.drain()) == 1


_INNER_TESTS = '''
import subprocess

import pytest

from ai_quota_swap import launch_agent

PROBE = "gui/501/com.example.qswap-launchd-guard-probe"


@pytest.fixture
def ran(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: seen.append(argv))
    monkeypatch.setattr(subprocess, "Popen", lambda argv, *a, **k: seen.append(argv))
    return seen


def test_swallowed(ran):
    try:
        launch_agent._launchctl("bootout", PROBE)
    except Exception:
        pass
    assert ran == []


def test_read_only(ran):
    assert launch_agent._launchctl("print", PROBE).returncode != 0
    assert ran == []
'''


def test_a_refusal_fails_its_test_even_when_the_code_swallows_it(tmp_path):
    """End to end, in a child pytest running this same conftest (as
    test_network_block.py does): catching the refusal does not save the test.
    The child's tests replace subprocess with recorders, so nothing runs."""
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    shutil.copy(conftest.__file__, inner / "conftest.py")
    (inner / "test_inner.py").write_text(textwrap.dedent(_INNER_TESTS), encoding="utf-8")

    src = str(Path(__file__).resolve().parent.parent / "src")
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("PYTEST_") and k != "PYTHONDONTWRITEBYTECODE"
    }
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "-rfE",
            "-p", "no:cacheprovider", "-p", "no:xdist",
            "-c", str(inner / "pytest.ini"), "--rootdir", str(inner),
            "--basetemp", str(tmp_path / "basetemp"),
            str(inner / "test_inner.py"),
        ],
        capture_output=True, text=True, env=env, cwd=inner, timeout=120,
    )
    out = result.stdout + result.stderr

    assert "1 failed, 1 passed" in out, out
    assert "FAILED test_inner.py::test_swallowed" in out, out
    assert "A test reached launchctl, which the suite refuses" in out, out
    assert "reached launchctl bootout unfaked" in out, out


@pytest.mark.parametrize(
    ("event", "args"),
    [
        ("subprocess.Popen", ("/bin/ps", ["ps", "-p", "1", "-o", "etime="], None, None)),
        ("subprocess.Popen", ("/usr/bin/sfltool", ["sfltool", "dumpbtm"], None, None)),
        ("subprocess.Popen", (sys.executable, [sys.executable, "-m", "ai_quota_swap",
                                               "--version"], None, None)),
        ("os.system", ("echo launchctl-free",)),
    ],
)
def test_the_audit_hook_lets_every_other_process_through(event, args, launchctl_guard):
    sys.audit(event, *args)

    assert launchctl_guard.drain() == []
