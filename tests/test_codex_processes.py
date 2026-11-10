"""Codex process detection and the background-server restart, with every
subprocess faked. The real functions are bound here at import, before the
conftest fixture replaces them on the module for every other test."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_quota_swap import clis
from ai_quota_swap.codex import processes
from ai_quota_swap.codex.processes import (
    BACKGROUND_SERVER,
    CLI,
    DESKTOP,
    classify,
)

real_list = processes.list_codex_processes
real_daemon_pid = processes.background_server_pid
real_restart = processes.restart_background_server


@pytest.mark.parametrize("args, kind", [
    ("/opt/homebrew/bin/codex", CLI),
    ("/usr/local/lib/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex resume", CLI),
    ("codex app-server --listen unix:// --managed-daemon", BACKGROUND_SERVER),
    ("/x/codex app-server --listen unix://", BACKGROUND_SERVER),
    ("codex app-server proxy", CLI),
    ("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex app-server", DESKTOP),
    ("/Applications/Codex.app/Contents/MacOS/Codex", DESKTOP),
    ("node /usr/local/lib/node_modules/@openai/codex/bin/codex.js", None),
    ("/Applications/ChatGPT.app/Contents/MacOS/ChatGPT", None),
    ("/usr/bin/vim codex.md", None),
    ("python -m codex", None),
])
def test_classify(args, kind):
    assert classify(args) == kind


class TestList:
    def test_parses_ps_and_skips_this_process(self, monkeypatch):
        own = os.getpid()
        listing = "\n".join([
            "  101 /usr/local/bin/codex",
            "  102 codex app-server --listen unix:// --managed-daemon",
            "  103 /Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex app-server",
            "  104 /bin/zsh",
            f"  {own} /usr/local/bin/codex",
            "garbage line",
        ])
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout=listing, stderr="")

        monkeypatch.setattr(processes.spawn, "run", fake_run)
        monkeypatch.setattr(processes, "process_codex_home", lambda pid: Path(f"/homes/{pid}"))
        monkeypatch.setattr(sys, "platform", "darwin")
        found = real_list()
        assert [(p.pid, p.kind, p.codex_home) for p in found] == [
            (101, CLI, Path("/homes/101")),
            (102, BACKGROUND_SERVER, Path("/homes/102")),
            (103, DESKTOP, None),
        ]
        assert calls[0][:2] == ["ps", "-axww"]

    def test_failures_read_as_nothing_running(self, monkeypatch):
        def boom(cmd, **kwargs):
            raise OSError("no ps")

        monkeypatch.setattr(processes.spawn, "run", boom)
        monkeypatch.setattr(sys, "platform", "linux")
        assert real_list() == []
        monkeypatch.setattr(sys, "platform", "win32")
        assert real_list() == []


class TestProcessHome:
    def test_macos_environment(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "darwin")
        out = f"/usr/local/bin/codex PATH=/bin HOME={tmp_path} CODEX_HOME={tmp_path}/work-home TERM=x"
        monkeypatch.setattr(processes.spawn, "run",
                            lambda cmd, **k: SimpleNamespace(returncode=0, stdout=out, stderr=""))
        assert processes.process_codex_home(5) == (tmp_path / "work-home").resolve()

    def test_macos_default_home(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "darwin")
        out = f"/usr/local/bin/codex PATH=/bin HOME={tmp_path}"
        monkeypatch.setattr(processes.spawn, "run",
                            lambda cmd, **k: SimpleNamespace(returncode=0, stdout=out, stderr=""))
        assert processes.process_codex_home(5) == tmp_path / ".codex"

    def test_macos_hidden_environment_is_unknown(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(processes.spawn, "run",
                            lambda cmd, **k: SimpleNamespace(returncode=0, stdout="/x/codex", stderr=""))
        assert processes.process_codex_home(5) is None

    def test_linux_environ(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "linux")
        environ = f"HOME={tmp_path}\0CODEX_HOME={tmp_path}/h2\0".encode()
        real_read = Path.read_bytes
        # as_posix: on Windows str(Path("/proc/7/environ")) has backslashes.
        monkeypatch.setattr(Path, "read_bytes",
                            lambda self: environ if self.as_posix() == "/proc/7/environ" else real_read(self))
        assert processes.process_codex_home(7) == (tmp_path / "h2").resolve()


class TestBackgroundServerPid:
    def test_reads_the_json_pid_record(self, tmp_path, monkeypatch):
        state = tmp_path / "app-server-daemon"
        state.mkdir()
        (state / "daemon.pid").write_text(json.dumps({"pid": 4242, "processStartTime": "x"}))
        monkeypatch.setattr(processes, "is_pid_alive", lambda pid: pid == 4242)
        assert real_daemon_pid(tmp_path) == 4242

    def test_legacy_name_and_dead_or_empty_records(self, tmp_path, monkeypatch):
        state = tmp_path / "app-server-daemon"
        state.mkdir()
        (state / "daemon.pid").write_text("")  # a start-up reservation
        (state / "app-server.pid").write_text(json.dumps({"pid": 77}))
        monkeypatch.setattr(processes, "is_pid_alive", lambda pid: pid == 77)
        assert real_daemon_pid(tmp_path) == 77
        monkeypatch.setattr(processes, "is_pid_alive", lambda pid: False)
        assert real_daemon_pid(tmp_path) is None

    def test_no_state_dir(self, tmp_path):
        assert real_daemon_pid(tmp_path) is None


class TestRestart:
    def test_runs_daemon_restart_on_the_home(self, monkeypatch, tmp_path):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"], seen["env"], seen["timeout"] = cmd, kwargs["env"], kwargs["timeout"]
            return SimpleNamespace(returncode=0, stdout="restarted\n", stderr="")

        monkeypatch.setattr(clis, "find_cli", lambda name: "/fake/bin/codex")
        monkeypatch.setattr(processes.spawn, "run", fake_run)
        result = real_restart(tmp_path, timeout=7)
        assert result.ok is True
        assert seen["cmd"] == ["/fake/bin/codex", "app-server", "daemon", "restart"]
        assert seen["env"]["CODEX_HOME"] == str(tmp_path)
        assert seen["timeout"] == 7

    def test_failures_are_results_never_exceptions(self, monkeypatch, tmp_path):
        monkeypatch.setattr(clis, "find_cli", lambda name: None)
        assert real_restart(tmp_path).detail == "codex is not on PATH"

        monkeypatch.setattr(clis, "find_cli", lambda name: "/fake/bin/codex")

        def timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        monkeypatch.setattr(processes.spawn, "run", timeout)
        result = real_restart(tmp_path, timeout=3)
        assert (result.ok, result.detail) == (False, "timed out after 3s")

        monkeypatch.setattr(processes.spawn, "run",
                            lambda cmd, **k: SimpleNamespace(returncode=2, stdout="", stderr="boom\n"))
        assert real_restart(tmp_path) == processes.RestartResult(False, "boom")
