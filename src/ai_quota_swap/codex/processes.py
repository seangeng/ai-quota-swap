"""Which Codex processes hold a login in memory, and restarting the background server.

A running Codex process never adopts a different account from a swapped
``auth.json``: it keeps the old token until it expires and then stops with a
"signed in to another account" error (aqs-notes ``codex-protocol.md`` §3). Two
holders outlive single sessions (§3, §8):

- the per-home background server the interactive TUI attaches to
  (``codex app-server --listen unix:// [--managed-daemon]``), whose pid record
  is ``$CODEX_HOME/app-server-daemon/daemon.pid`` (``app-server.pid`` for an
  older managed install). ``codex app-server daemon restart`` restarts it.
- the ChatGPT desktop app, which runs its bundled codex
  (``…/Contents/Resources/codex-cli/…``) on ``~/.codex``.

Everything here is best effort and never raises: a process listing that cannot
be read is an empty one, a home that cannot be told is ``None`` (callers treat
it as "maybe this home"). The functions are looked up through this module at
call time, so tests replace them here.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ai_quota_swap import clis
from ai_quota_swap import spawn
from ai_quota_swap.process_detection import is_pid_alive

_logger = logging.getLogger("ai-quota-swap")

DAEMON_STATE_DIR = "app-server-daemon"
#: Current and legacy pid record names (``app-server-daemon/src/lib.rs``).
DAEMON_PID_FILES = ("daemon.pid", "app-server.pid")
RESTART_TIMEOUT_S = 30.0

CLI = "cli"
BACKGROUND_SERVER = "app-server"
DESKTOP = "desktop"

#: Path pieces that mark the desktop app's bundled codex and its helpers.
_DESKTOP_MARKERS = ("/Contents/Resources/codex-cli/", "/Codex.app/Contents/")


@dataclass(frozen=True)
class CodexProcess:
    pid: int
    kind: str  # CLI | BACKGROUND_SERVER | DESKTOP
    codex_home: Path | None  # None: could not tell


@dataclass(frozen=True)
class RestartResult:
    ok: bool
    detail: str = ""


def classify(args: str) -> str | None:
    """What kind of Codex process a command line is, or None for anything else."""
    if any(marker in args for marker in _DESKTOP_MARKERS):
        return DESKTOP
    argv0 = args.split(" ", 1)[0]
    name = os.path.basename(argv0).lower()
    if name in ("node", "bun", "deno") and "@openai/codex" in args:
        # The npm wrapper only spawns the native binary, which is listed on
        # its own and is what holds the login.
        return None
    if name not in ("codex", "codex.exe"):
        return None
    if " app-server" in args and ("--managed-daemon" in args or "--listen" in args):
        return BACKGROUND_SERVER
    return CLI


def _environment_home(env: dict[str, str]) -> Path:
    value = env.get("CODEX_HOME")
    if value:
        try:
            return Path(value).expanduser().resolve()
        except OSError:
            return Path(value)
    home = env.get("HOME")
    base = Path(home) if home else Path.home()
    return base / ".codex"


def process_codex_home(pid: int) -> Path | None:
    """The Codex home a process runs on, from its environment; None if unreadable."""
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            return None
        env = {}
        for item in raw.split(b"\0"):
            key, sep, value = item.partition(b"=")
            if sep:
                env[key.decode(errors="replace")] = value.decode(errors="replace")
        return _environment_home(env)
    if sys.platform == "darwin":
        try:
            proc = spawn.run(
                ["ps", "-E", "-ww", "-o", "command=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        text = proc.stdout.strip()
        if proc.returncode != 0 or " HOME=" not in f" {text}":
            return None  # environment not visible (another user's process)
        env = {}
        for token in text.split(" "):
            key, sep, value = token.partition("=")
            if sep and key.isidentifier() and key.isupper():
                env.setdefault(key, value)
        return _environment_home(env)
    return None


def list_codex_processes() -> list[CodexProcess]:
    """Every running Codex process this user can see (POSIX); [] on Windows."""
    if sys.platform == "win32":
        return []
    try:
        proc = spawn.run(
            ["ps", "-axww", "-o", "pid=,args="],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _logger.debug("Codex process listing failed: %r", e)
        return []
    if proc.returncode != 0:
        return []
    own = os.getpid()
    found: list[CodexProcess] = []
    for line in proc.stdout.splitlines():
        pid_text, _, args = line.strip().partition(" ")
        if not pid_text.isdigit():
            continue
        pid = int(pid_text)
        if pid == own:
            continue
        kind = classify(args.strip())
        if kind is None:
            continue
        home = None if kind == DESKTOP else process_codex_home(pid)
        found.append(CodexProcess(pid=pid, kind=kind, codex_home=home))
    return found


def background_server_pid(home: Path) -> int | None:
    """Pid of the background server running on ``home``, or None."""
    state = home / DAEMON_STATE_DIR
    for name in DAEMON_PID_FILES:
        try:
            text = (state / name).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue  # a start-up reservation, not a running server
        pid = None
        try:
            record = json.loads(text)
        except ValueError:
            record = None
        if isinstance(record, dict) and isinstance(record.get("pid"), int):
            pid = record["pid"]
        elif isinstance(record, int):
            pid = record
        if pid is not None and pid > 1 and is_pid_alive(pid):
            return pid
    return None


@dataclass(frozen=True)
class LoginRun:
    """How ``codex login`` ended: its exit code, or why it never ran."""

    returncode: int | None
    detail: str = ""


def run_codex_login(home: Path, args: list[str]) -> LoginRun:
    """``codex login [args]`` with ``CODEX_HOME=home``, on the user's terminal.

    Stdin, stdout and stderr are inherited, so the browser hand-off or the
    device code shows as usual. Ctrl-C reaches ``codex`` too (same process
    group) and comes back here as ``KeyboardInterrupt``. Only the child's
    environment names ``home``; this process's ``CODEX_HOME`` is untouched.
    """
    exe = clis.find_cli("codex")
    if exe is None:
        return LoginRun(None, "codex is not on PATH")
    env = {**os.environ, "CODEX_HOME": str(home)}
    try:
        proc = spawn.run([exe, "login", *args], env=env, check=False)
    except OSError as e:
        return LoginRun(None, str(e))
    return LoginRun(proc.returncode)


def restart_background_server(home: Path, timeout: float = RESTART_TIMEOUT_S) -> RestartResult:
    """``codex app-server daemon restart`` on ``home``. Never raises."""
    exe = clis.find_cli("codex")
    if exe is None:
        return RestartResult(False, "codex is not on PATH")
    env = {**os.environ, "CODEX_HOME": str(home)}
    try:
        proc = spawn.run(
            [exe, "app-server", "daemon", "restart"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return RestartResult(False, f"timed out after {timeout:g}s")
    except OSError as e:
        return RestartResult(False, str(e))
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    detail = tail[-1] if tail else ""
    if proc.returncode != 0:
        return RestartResult(False, detail or f"exit code {proc.returncode}")
    return RestartResult(True, detail)
