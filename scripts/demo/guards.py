"""The demo harness's sandbox: nothing a demo run does may reach real accounts.

This machine holds real logins, and on macOS the tool reads them from the
login Keychain, which is not scoped by ``$HOME``. Running the real CLI with a
fake home would still read the real Claude Code login and call Anthropic with
it. So a demo process installs every guard below before it imports anything
that touches accounts, checks each one, and refuses to start when any check
fails. Nothing here falls back to real behaviour: a guard that cannot install
is a run that does not happen.

The layers, in install order:

1. A fresh temp home. ``HOME``, ``USERPROFILE``, ``CLAUDE_CONFIG_DIR``,
   ``CODEX_HOME``, ``XDG_*`` and ``TMPDIR`` all point inside it, the rest of
   the inherited environment is dropped (no tokens or keys ride along), and
   the working directory moves into it.
2. A permanent audit hook (``sys.addaudithook`` cannot be removed). It refuses
   any file access, read or write, under the real account stores (``~/.claude*``,
   ``~/.codex``, ``~/.ai-quota-swap``, ``~/.claude-swap-backup``, the Keychain
   directories, ...); any write outside the temp home and the declared output
   files; every process spawn (``/usr/bin/security`` included); and every
   connection or name lookup that leaves the machine. It fires before the
   operation, so a refused open never reaches the filesystem.
3. A socket-level block on outbound connections (loopback allowed), the same
   idea as the test suite's, as a second layer under the hook.
4. The Keychain layer (``ai_quota_swap.macos_keychain``) replaced by an
   in-memory store.
5. ``urllib.request.urlopen``, the one function every usage, profile, refresh
   and reset call in the package goes through, replaced by the canned API in
   ``fakeapi.py``.

``verify()`` checks all of it, including that the hook actually refuses (it
raises synthetic audit events at it, so the self-test never touches a real
file or socket). Violations that code under test swallows (a refused
connection is an ``OSError``, so fetch paths take their offline branch) are
recorded, and the launcher exits non-zero when any were.

This module imports nothing from ``ai_quota_swap`` at import time.
"""

from __future__ import annotations

import atexit
import errno
import ipaddress
import json
import os
import pwd
import socket
import sys
import tempfile
import threading
import types
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]

# Environment variables kept from the parent process. Everything else is
# dropped: the demo has no use for credentials, cloud keys or proxies. The
# TEXTUAL_*/COLUMNS/ROWS group is what textual-serve hands the app it starts.
_KEEP_ENV = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM",
    "TEXTUAL_DRIVER", "TEXTUAL_FPS", "TEXTUAL_COLOR_SYSTEM", "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION", "COLUMNS", "ROWS", "LINES",
)

# Under each real home, the first path component that marks a real store.
# A component that starts with one of the prefixes is protected whatever
# follows (``.claude``, ``.claude.json``, ``.claude.json.lock``, ``.claude-swap-backup``).
_PROTECTED_PREFIXES = (".claude",)
_PROTECTED_NAMES = (
    ".codex",
    ".ai-quota-swap",
    ".littledemo",
    ".screenframed.json",
    ".ssh",
    ".aws",
    ".netrc",
)
_PROTECTED_SUBPATHS = (
    (".local", "share", "ai-quota-swap"),
    (".local", "share", "claude"),
    (".config", "claude"),
    ("Library", "Keychains"),
    ("Library", "Application Support", "Claude"),
)
_SYSTEM_PROTECTED = (Path("/Library/Keychains"), Path("/private/var/db/mds"))

_WRITE_EVENTS = frozenset({
    "os.rename", "os.remove", "os.rmdir", "os.mkdir", "shutil.rmtree", "os.symlink",
    "os.link", "os.truncate", "os.chmod", "os.chown", "os.utime", "os.chflags",
    "os.lchflags", "os.setxattr", "os.removexattr", "shutil.copyfile", "shutil.copytree",
    "shutil.move", "tempfile.mkstemp", "tempfile.mkdtemp",
})
_READ_EVENTS = frozenset({"os.listdir", "os.scandir", "glob.glob", "os.listxattr", "os.getxattr"})
_SPAWN_EVENTS = frozenset({
    "subprocess.Popen", "os.posix_spawn", "os.exec", "os.spawn", "os.system", "os.fork",
    "os.forkpty", "os.startfile", "pty.spawn", "webbrowser.open",
})
_NET_EVENTS = frozenset({
    "socket.connect", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo",
    "socket.gethostbyname", "socket.gethostbyname_ex", "socket.gethostbyaddr",
    "socket.getnameinfo", "http.client.connect", "urllib.Request",
})


class DemoGuardError(RuntimeError):
    """A guard did not install or failed its self-test: the run must not start."""


class DemoGuardViolation(Exception):
    """A demo process tried to cross the sandbox.

    Deliberately not an ``OSError``: the package catches ``OSError`` in many
    places to degrade gracefully, and a refusal must never be absorbed as
    "file not there, carry on". The run dies loudly instead.
    """


class NetworkBlocked(OSError):
    """An outbound connection or name lookup was refused.

    An ``OSError`` on purpose (like the test suite's ``NetworkAccessBlocked``):
    fetch paths then take their offline branch instead of crashing a thread.
    Every attempt is recorded, and the launcher fails the run afterwards.
    """


@dataclass
class Sandbox:
    """State of the installed guards, for ``verify`` and the launcher."""

    home: Path
    real_homes: tuple[Path, ...]
    protected_roots: tuple[Path, ...]
    write_roots: list[Path]
    allow_spawn: object = None  # callable(event, args) -> bool, or None
    violations: list[str] = field(default_factory=list)
    audit: list[dict] | None = None
    keychain: object = None
    selftest: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    owns_home: bool = True

    def record_violation(self, message: str) -> None:
        if self.selftest:
            return
        with self.lock:
            self.violations.append(message)


_SANDBOX: Sandbox | None = None


def current() -> Sandbox:
    if _SANDBOX is None:
        raise DemoGuardError("the demo sandbox is not installed")
    return _SANDBOX


# -- path classification -----------------------------------------------------


def _norm(path: str) -> str:
    return os.path.normpath(os.path.join(os.getcwd(), path)) if not os.path.isabs(path) else os.path.normpath(path)


def _spellings(path: str) -> tuple[str, ...]:
    """The path as written (normalized) and as the filesystem resolves it."""
    normal = _norm(path)
    try:
        real = os.path.realpath(normal)
    except (OSError, ValueError):
        real = normal
    return (normal,) if real == normal else (normal, real)


def _under(path: str, root: Path) -> bool:
    r = str(root)
    return path == r or path.startswith(r.rstrip(os.sep) + os.sep)


def _is_protected_spelling(path: str, sb: Sandbox) -> bool:
    if _under(path, sb.home) or any(_under(path, Path(p)) for p in _home_aliases(sb.home)):
        return False
    for root in sb.protected_roots:
        if _under(path, root):
            return True
    for home in sb.real_homes:
        if not _under(path, home) or path == str(home):
            continue
        rel = Path(path).relative_to(home).parts
        if not rel:
            continue
        first = rel[0]
        if any(first.startswith(p) for p in _PROTECTED_PREFIXES) or first in _PROTECTED_NAMES:
            return True
        for sub in _PROTECTED_SUBPATHS:
            if tuple(rel[: len(sub)]) == sub:
                return True
    return False


def is_protected(path: str, sb: Sandbox | None = None) -> bool:
    sb = sb or current()
    return any(_is_protected_spelling(p, sb) for p in _spellings(path))


def _home_aliases(home: Path) -> tuple[str, ...]:
    # /var -> /private/var on macOS: the temp home has two spellings.
    s = str(home)
    out = {s}
    if s.startswith("/private/"):
        out.add(s[len("/private"):])
    elif s.startswith(("/var/", "/tmp/")):
        out.add("/private" + s)
    return tuple(out)


_DEVICE_WRITES = ("/dev/null", "/dev/tty", "/dev/stdout", "/dev/stderr")


def is_writable(path: str, sb: Sandbox | None = None) -> bool:
    sb = sb or current()
    for spelling in _spellings(path):
        if spelling in _DEVICE_WRITES:
            return True
        if any(_under(spelling, Path(a)) for a in _home_aliases(sb.home)):
            continue
        if any(_under(spelling, root) for root in sb.write_roots):
            continue
        return False
    return True


# -- network classification --------------------------------------------------


def is_loopback_host(host: object) -> bool:
    """Whether a host names this machine (loopback or localhost). Mirrors the
    test suite's rule, minus the unspecified address: connecting to 0.0.0.0
    is refused here too."""
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    name = host.strip("[]").split("%", 1)[0].lower()
    if name in ("localhost",) or name.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _address_allowed(family: object, address: object, sb: Sandbox) -> bool:
    if family == getattr(socket, "AF_UNIX", object()):
        path = address if isinstance(address, str) else os.fsdecode(address) if isinstance(address, bytes) else ""
        # Unix sockets stay on the machine, but only ones inside the sandbox
        # are ours; a daemon's socket elsewhere is not the demo's business.
        return bool(path) and is_writable(path, sb)
    if family is not None and family not in (socket.AF_INET, socket.AF_INET6):
        return False
    host = address[0] if isinstance(address, tuple) and address else address
    return is_loopback_host(host)


# -- the audit hook ----------------------------------------------------------


def _fspath(value: object) -> str | None:
    if value is None or isinstance(value, int):
        return None
    try:
        p = os.fspath(value)
    except TypeError:
        return None
    if isinstance(p, bytes):
        try:
            return os.fsdecode(p)
        except (UnicodeDecodeError, ValueError):
            return str(p)
    return p


def _is_write_open(mode: object, flags: object) -> bool:
    if isinstance(mode, str):
        return any(c in mode for c in "wax+")
    if not isinstance(flags, int):
        return False
    if flags & (os.O_WRONLY | os.O_RDWR):
        return True
    return bool(flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND))


def _note(sb: Sandbox, event: str, **detail: object) -> None:
    if sb.audit is not None and not sb.selftest:
        with sb.lock:
            sb.audit.append({"event": event, **detail})


def _refuse(sb: Sandbox, message: str) -> DemoGuardViolation:
    sb.record_violation(message)
    return DemoGuardViolation(message)


def _check_read(sb: Sandbox, event: str, path: str) -> None:
    if is_protected(path, sb):
        raise _refuse(sb, f"{event} refused: {path} is a real account store")


def _check_write(sb: Sandbox, event: str, path: str) -> None:
    if is_protected(path, sb):
        raise _refuse(sb, f"{event} refused: {path} is a real account store")
    if not is_writable(path, sb):
        raise _refuse(sb, f"{event} refused: {path} is outside the demo's temp home")


def _audit_hook(event: str, args: tuple) -> None:
    sb = _SANDBOX
    if sb is None:
        return
    if event == "open":
        path = _fspath(args[0]) if args else None
        if path is None:
            return
        write = _is_write_open(args[1] if len(args) > 1 else None, args[2] if len(args) > 2 else 0)
        _note(sb, "open", path=_norm(path), write=write)
        if write:
            _check_write(sb, event, path)
        else:
            _check_read(sb, event, path)
        return
    if event in _READ_EVENTS:
        path = _fspath(args[0]) if args else None
        if path is None:
            return
        _note(sb, event, path=_norm(path))
        _check_read(sb, event, path)
        return
    if event in _WRITE_EVENTS:
        if event in ("os.rename", "os.link", "shutil.copyfile", "shutil.copytree", "shutil.move"):
            targets = [args[0], args[1]] if event == "os.rename" or event == "shutil.move" else [args[1]]
            sources = [] if event in ("os.rename", "shutil.move") else [args[0]]
        elif event == "os.symlink":
            # args: (target, link, dir_fd). The link is created; its target
            # must not lead into a store either.
            targets, sources = [args[1]], [args[0]]
        else:
            targets, sources = [args[0]] if args else [], []
        for raw in sources:
            p = _fspath(raw)
            if p is not None:
                _note(sb, event, path=_norm(p), write=False)
                _check_read(sb, event, p)
        for raw in targets:
            p = _fspath(raw)
            if p is not None:
                _note(sb, event, path=_norm(p), write=True)
                _check_write(sb, event, p)
        return
    if event == "sqlite3.connect":
        path = _fspath(args[0]) if args else None
        if path is None or path == ":memory:" or path.startswith("file::memory:"):
            return
        if path.startswith("file:"):
            path = path[5:].split("?", 1)[0]
        _note(sb, event, path=_norm(path), write=True)
        _check_write(sb, event, path)  # sqlite opens files below the audit layer
        return
    if event in _SPAWN_EVENTS:
        allowed = sb.allow_spawn is not None and sb.allow_spawn(event, args)
        what = args[0] if args else ""
        _note(sb, event, target=str(what), allowed=bool(allowed))
        if not allowed:
            raise _refuse(sb, f"{event} refused: the demo spawns no processes ({what!r})")
        return
    if event in _NET_EVENTS:
        if event == "socket.connect" or event in ("socket.sendto", "socket.sendmsg"):
            sock, address = args[0], args[-1]
            if event != "socket.connect" and address is None:
                # A send on a connected socket: its peer was vetted at connect
                # time, or it was accepted on a loopback-only listener.
                return
            ok = _address_allowed(getattr(sock, "family", None), address, sb)
        elif event == "http.client.connect":
            ok = is_loopback_host(args[1] if len(args) > 1 else None)
            address = args[1:3]
        elif event == "urllib.Request":
            url = str(args[0]) if args else ""
            address = url
            ok = is_loopback_host(urlsplit(url).hostname)
        else:  # name lookups
            address = args[0] if args else None
            ok = address is None or is_loopback_host(address)
        _note(sb, event, address=repr(address), allowed=ok)
        if not ok:
            message = f"{event} refused: {address!r} is off this machine"
            sb.record_violation(message)
            raise NetworkBlocked(errno.ENETUNREACH, message)
        return
    if event == "socket.bind":
        sock, address = args[0], args[1]
        family = getattr(sock, "family", None)
        if family in (socket.AF_INET, socket.AF_INET6):
            host = address[0] if isinstance(address, tuple) and address else address
            ok = is_loopback_host(host)
            _note(sb, event, address=repr(address), allowed=ok)
            if not ok:
                raise _refuse(sb, f"{event} refused: the demo serves on loopback only ({address!r})")
        return
    if event == "ctypes.dlopen":
        name = str(args[0]) if args else ""
        _note(sb, event, target=name)
        if "Security.framework" in name or "libsecurity" in name.lower():
            raise _refuse(sb, f"{event} refused: {name} is the Keychain framework")
        return


# -- socket layer (second line, as in tests/conftest.py) ---------------------

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection


def _guarded_connect(self, address):
    sb = current()
    if not _address_allowed(self.family, address, sb):
        message = f"socket.connect refused: {address!r} is off this machine"
        sb.record_violation(message)
        raise NetworkBlocked(errno.ENETUNREACH, message)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    sb = current()
    if not _address_allowed(self.family, address, sb):
        sb.record_violation(f"socket.connect_ex refused: {address!r} is off this machine")
        return errno.ECONNREFUSED
    return _real_connect_ex(self, address)


def _guarded_create_connection(address, *args, **kwargs):
    sb = current()
    if not _address_allowed(None, address, sb):
        message = f"socket.create_connection refused: {address!r} is off this machine"
        sb.record_violation(message)
        raise NetworkBlocked(errno.ENETUNREACH, message)
    return _real_create_connection(address, *args, **kwargs)


def _no_proxies() -> dict:
    # A proxy on loopback would carry a request off the machine over a
    # connection the block allows.
    return {}


# -- install -----------------------------------------------------------------


def _real_homes() -> tuple[Path, ...]:
    homes: list[Path] = []
    for raw in (os.environ.get("HOME"), os.environ.get("USERPROFILE")):
        if raw:
            homes.append(Path(raw))
    try:
        homes.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except (KeyError, OSError):
        pass
    out: list[Path] = []
    for h in homes:
        for spelling in {os.path.normpath(str(h)), os.path.realpath(str(h))}:
            p = Path(spelling)
            if p not in out and str(p) != "/":
                out.append(p)
    return tuple(out)


def _ambient_store_roots() -> tuple[Path, ...]:
    roots = list(_SYSTEM_PROTECTED)
    for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "CODEX_HOME"):
        raw = os.environ.get(name)
        if raw:
            roots.append(Path(os.path.normpath(os.path.expanduser(raw))))
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        base = Path(os.path.normpath(os.path.expanduser(xdg)))
        roots += [base / "ai-quota-swap", base / "claude"]
    return tuple(roots)


def _set_env(home: Path, extra: dict[str, str]) -> None:
    kept = {k: os.environ[k] for k in _KEEP_ENV if k in os.environ}
    os.environ.clear()
    os.environ.update(kept)
    tmp = home / "tmp"
    tmp.mkdir(exist_ok=True)
    os.environ.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CODEX_HOME": str(home / ".codex"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "TMPDIR": str(tmp),
        # The Keychain item's account name and anything else that reads the
        # user name: an invented one, so the real one cannot reach a capture.
        "USER": "demo",
        "LOGNAME": "demo",
        # Claude Code's version for the reset-status agent string: fixed, so
        # nothing goes looking for the installed `claude` binary.
        "QSWAP_CLAUDE_CLI_VERSION": "2.1.321",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TZ": "UTC",
    })
    os.environ.update(extra)
    tempfile.tempdir = None  # re-read TMPDIR
    try:
        import time

        time.tzset()
    except AttributeError:  # pragma: no cover - Windows
        pass


class MemoryKeychain:
    """In-memory ``(service, account) -> secret`` map with the
    ``ai_quota_swap.macos_keychain`` contract (absent = None, delete is a no-op)."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}
        self.calls = 0

    def get_password(self, service: str, account: str) -> str | None:
        self.calls += 1
        return self.data.get((service, account))

    def item_exists(self, service: str, account: str) -> bool:
        self.calls += 1
        return (service, account) in self.data

    def set_password(self, service: str, account: str, password: str) -> None:
        self.calls += 1
        self.data[(service, account)] = password

    def delete_password(self, service: str, account: str) -> None:
        self.calls += 1
        self.data.pop((service, account), None)


def install(
    *,
    extra_env: dict[str, str] | None = None,
    write_roots: tuple[Path, ...] = (),
    audit: bool = False,
    allow_spawn=None,
    import_package: bool = True,
) -> Sandbox:
    """Install every guard. Call once, first thing, before importing the package.

    ``write_roots``: files or directories outside the temp home this process
    may write (the launcher's HTML output). ``allow_spawn``: a predicate for
    the one process that spawns on purpose (the web server, which starts a
    guarded launcher per browser session). ``import_package`` False skips the
    package-level guards, for that server, which never imports the package.
    """
    global _SANDBOX
    if _SANDBOX is not None:
        raise DemoGuardError("the demo sandbox is already installed in this process")
    sys.dont_write_bytecode = True

    real_homes = _real_homes()
    protected = _ambient_store_roots()
    home = Path(tempfile.mkdtemp(prefix="qswap-demo-")).resolve()
    for sub in (".claude", ".codex", ".local/share", ".config", ".cache", ".local/state"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    _set_env(home, extra_env or {})
    os.chdir(home)

    sb = Sandbox(
        home=home,
        real_homes=real_homes,
        protected_roots=protected,
        write_roots=[Path(os.path.realpath(str(p))) for p in write_roots]
        + [Path(os.path.normpath(str(p))) for p in write_roots],
        allow_spawn=allow_spawn,
        audit=[] if audit else None,
    )
    _SANDBOX = sb
    sys.addaudithook(_audit_hook)

    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.create_connection = _guarded_create_connection
    import urllib.request

    urllib.request.getproxies = _no_proxies

    if import_package:
        from ai_quota_swap import macos_keychain

        keychain = MemoryKeychain()
        for name in ("get_password", "item_exists", "set_password", "delete_password"):
            setattr(macos_keychain, name, getattr(keychain, name))
        sb.keychain = keychain

        # The menu bar's wrapper app lives in /Applications, outside $HOME.
        from ai_quota_swap import app_bundle

        app_bundle.APPLICATIONS_DIR = home / "Applications"

    atexit.register(_cleanup_home, sb)
    return sb


def install_fake_api(api) -> None:
    """Route every ``urllib.request.urlopen`` call to ``api`` (``fakeapi.FakeAnthropic``)."""
    import urllib.request

    urllib.request.urlopen = api.urlopen


def _cleanup_home(sb: Sandbox) -> None:
    if not sb.owns_home or os.environ.get("QSWAP_DEMO_KEEP_HOME"):
        return
    # A plain absolute walk: shutil.rmtree's fd-relative unlinks reach the
    # hook as bare names.
    try:
        os.chdir(sb.home.parent)
    except OSError:
        return
    for dirpath, dirnames, filenames in os.walk(sb.home, topdown=False):
        for name in filenames:
            try:
                os.remove(os.path.join(dirpath, name))
            except OSError:
                pass
        for name in dirnames:
            p = os.path.join(dirpath, name)
            try:
                if os.path.islink(p):
                    os.remove(p)
                else:
                    os.rmdir(p)
            except OSError:
                pass
    try:
        os.rmdir(sb.home)
    except OSError:
        pass


# -- verify ------------------------------------------------------------------


def _expect_refusal(sb: Sandbox, event: str, args: tuple, kind: type[BaseException]) -> str | None:
    sb.selftest = True
    try:
        sys.audit(event, *args)
    except kind:
        return None
    except Exception as e:  # noqa: BLE001
        return f"audit hook answered {event} with {type(e).__name__}: {e}"
    finally:
        sb.selftest = False
    return f"audit hook let {event} {args[0]!r} through"


def verify(sb: Sandbox, *, clock_epoch: float | None = None, package: bool = True) -> None:
    """Check every guard; raise ``DemoGuardError`` listing each failure."""
    failures: list[str] = []
    home = sb.home

    def inside(label: str, value: object) -> None:
        p = _fspath(value)
        if p is None or not any(_under(os.path.realpath(p), Path(a)) or _under(_norm(p), Path(a)) for a in _home_aliases(home)):
            failures.append(f"{label} is {value!r}, not inside the temp home {home}")

    for name in ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_DATA_HOME",
                 "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "TMPDIR"):
        inside(f"${name}", os.environ.get(name))
    inside("Path.home()", Path.home())
    inside("expanduser('~')", os.path.expanduser("~"))
    inside("tempfile.gettempdir()", tempfile.gettempdir())
    inside("cwd", os.getcwd())
    for name in ("CLAUDE_SECURESTORAGE_CONFIG_DIR", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                 "OPENAI_API_KEY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        if os.environ.get(name) is not None:
            failures.append(f"${name} survived the environment scrub")

    if socket.socket.connect is not _guarded_connect:
        failures.append("socket.socket.connect is not guarded")
    if socket.socket.connect_ex is not _guarded_connect_ex:
        failures.append("socket.socket.connect_ex is not guarded")
    if socket.create_connection is not _guarded_create_connection:
        failures.append("socket.create_connection is not guarded")
    import urllib.request

    if urllib.request.getproxies() != {}:
        failures.append("urllib still sees a proxy")

    probe = "qswap-demo-guard-probe-never-created"
    for real in sb.real_homes[:1]:
        for target in (real / ".claude" / probe, real / ".claude.json", real / ".codex" / probe,
                       real / ".ai-quota-swap" / probe, real / "Library" / "Keychains" / probe):
            msg = _expect_refusal(sb, "open", (str(target), "r", os.O_RDONLY), DemoGuardViolation)
            if msg:
                failures.append(msg)
        msg = _expect_refusal(sb, "os.listdir", (str(real / ".claude"),), DemoGuardViolation)
        if msg:
            failures.append(msg)
        msg = _expect_refusal(
            sb, "open", (str(real / probe), "w", os.O_WRONLY | os.O_CREAT), DemoGuardViolation
        )
        if msg:
            failures.append(msg)
    msg = _expect_refusal(
        sb, "subprocess.Popen",
        ("/usr/bin/security", ["/usr/bin/security", "find-generic-password"], None, None),
        DemoGuardViolation,
    )
    if msg:
        failures.append(msg)
    fake_sock = types.SimpleNamespace(family=socket.AF_INET)
    for event, args in (
        ("socket.connect", (fake_sock, ("192.0.2.1", 443))),
        ("socket.getaddrinfo", ("api.anthropic.com", 443, 0, 0, 0)),
        ("urllib.Request", ("https://api.anthropic.com/api/oauth/usage", None, {}, "GET")),
    ):
        msg = _expect_refusal(sb, event, args, NetworkBlocked)
        if msg:
            failures.append(msg)
    for host in ("127.0.0.1", "::1", "localhost"):
        if not is_loopback_host(host):
            failures.append(f"loopback {host} would be refused")

    if package:
        from ai_quota_swap import macos_keychain, paths

        kc = sb.keychain
        if not isinstance(kc, MemoryKeychain):
            failures.append("no in-memory Keychain installed")
        else:
            for name in ("get_password", "item_exists", "set_password", "delete_password"):
                if getattr(macos_keychain, name) != getattr(kc, name):
                    failures.append(f"macos_keychain.{name} is not the in-memory fake")
        for label, fn in (
            ("paths.get_claude_config_home()", paths.get_claude_config_home),
            ("paths.get_default_claude_config_home()", paths.get_default_claude_config_home),
            ("paths.get_global_config_path()", paths.get_global_config_path),
            ("paths.get_default_global_config_path()", paths.get_default_global_config_path),
            ("paths.get_credentials_path()", paths.get_credentials_path),
            ("paths.get_codex_home()", paths.get_codex_home),
            ("paths.get_backup_root()", paths.get_backup_root),
            ("paths.get_legacy_backup_root()", paths.get_legacy_backup_root),
        ):
            inside(label, fn())
        from ai_quota_swap import app_bundle

        inside("app_bundle.APPLICATIONS_DIR", app_bundle.APPLICATIONS_DIR)
        api = getattr(urllib.request.urlopen, "__self__", None)
        if type(api).__name__ != "FakeAnthropic":
            failures.append("urllib.request.urlopen is not the canned API")

    if clock_epoch is not None:
        import time

        if abs(time.time() - clock_epoch) > 120:
            failures.append(
                f"the clock is not frozen at the scenario time (off by {time.time() - clock_epoch:.0f}s)"
            )

    if failures:
        raise DemoGuardError("demo guards failed:\n  - " + "\n  - ".join(failures))


# -- audit log ---------------------------------------------------------------


def open_audit_log(path: str) -> int:
    """Open the audit log before the hook exists; written with os.write at exit."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)


def write_audit_log(fd: int, sb: Sandbox) -> None:
    payload = {
        "home": str(sb.home),
        "realHomes": [str(h) for h in sb.real_homes],
        "writeRoots": [str(p) for p in sb.write_roots],
        "events": sb.audit or [],
        "violations": sb.violations,
    }
    data = json.dumps(payload).encode()
    while data:
        n = os.write(fd, data)
        data = data[n:]
    os.close(fd)
