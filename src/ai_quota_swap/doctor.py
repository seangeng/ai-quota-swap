"""``qswap doctor``: a read-only self-check.

One line per check, each marked ``ok``, ``warn`` or ``fail``, and for anything
that does not pass, the command that fixes it on the next line
(docs/design/ux.md: every message says what to run next, fits 80 columns, uses
colour only together with a word). ``--json`` prints the same checks as data,
``--online`` adds two reachability checks; without it nothing touches the
network. Exit status 0 when no check fails, 1 when one does.

Claude Code checks that appear only when they apply: an ``apiKeyHelper`` in
Claude Code's user settings (qswap's own line is the active key; a line for an
account that cannot give a key, or run profiles left with a settings copy,
fail with the repair), a variable in qswap's environment that overrides the
active account (names only, never values), an API-key account that is active
as a saved key while sessions older than its activation are running, and
session records qswap cannot read (which stop a switch to an API-key account
whenever the key helper cannot be added).

Doctor never changes anything and never asks a question. In particular it
builds no switcher (constructing one creates the data directory and runs
migrations); it reads the files a switcher would read, directly. Everything
that touches the system (PATH, ``claude --version``, files, ``launchctl``, the
Keychain, the platform, the clock, the network) goes through :class:`Probes`,
so tests swap in fakes and run the same on every platform. Every probe is
called under ``except Exception``: one that fails, or that a sandbox refuses,
reads as "unknown" and never ends the command.

Check ids are stable names (``claude.cli``, ``codex.storage``, ...). Which
ones appear depends on the machine: menu bar checks only on macOS, the
permission check not on Windows, a provider's login checks only once its
command or files exist, the network checks only with ``--online``.
"""

from __future__ import annotations

import json
import os
import platform as _platform
import plistlib
import re
import stat
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ai_quota_swap import paths
from ai_quota_swap.models import Platform

#: The ``schemaVersion`` of ``qswap doctor --json``.
SCHEMA_VERSION = 1

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
STATUSES = (PASS, WARN, FAIL, SKIP)

#: Groups in output order: (JSON id, text heading).
GROUPS = (
    ("claude", "Claude Code"),
    ("codex", "Codex"),
    ("data", "Data directory"),
    ("menubar", "Menu bar"),
    ("auto", "Auto-switch"),
    ("install", "qswap"),
    ("network", "Network"),
)

#: The mark each status prints as.
MARKS = {PASS: "ok", WARN: "warn", FAIL: "fail", SKIP: "skip"}

#: Hosts the ``--online`` checks reach.
ANTHROPIC_URL = "https://api.anthropic.com/"
CHATGPT_URL = "https://chatgpt.com/"
NETWORK_TIMEOUT_S = 5.0

#: How long ``claude --version`` / ``codex --version`` may take.
VERSION_TIMEOUT_S = 10.0
#: How long ``launchctl print`` may take.
LAUNCHCTL_TIMEOUT_S = 5.0

#: A Keychain service nothing ever creates: looking it up answers "not found"
#: when the Keychain works, without decrypting or writing anything.
KEYCHAIN_PROBE_SERVICE = "ai-quota-swap-doctor-probe"

#: Folders macOS keeps from background services (privacy protection).
PROTECTED_FOLDERS = ("Documents", "Desktop", "Downloads")

CLAUDE_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"
CLAUDE_INSTALL_WINDOWS = "irm https://claude.ai/install.ps1 | iex"
CODEX_INSTALL = "npm install -g @openai/codex"

MENUBAR_LOG_HINT = "its log: ~/Library/Logs/com.aiquotaswap.menubar.err"

_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.]+)?")


# -- checks ------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One line of the report.

    ``fix`` is the command (or short instruction) that clears a ``warn`` or
    ``fail``; ``hint`` is a dim line for a passing check that has a next step
    anyway ("start one with qswap auto"). ``path``: the file the check is
    about, when the summary names it in words; printed whole on a line of
    its own (ux.md: a single path is never broken or cut).
    """

    id: str
    group: str
    status: str
    summary: str
    fix: str | None = None
    hint: str | None = None
    path: str | None = None

    def to_json(self) -> dict:
        out = {
            "id": self.id,
            "group": self.group,
            "status": self.status,
            "summary": self.summary,
            "fix": self.fix,
        }
        if self.path is not None:
            out["path"] = self.path
        return out


# -- probes ------------------------------------------------------------------------


def _read_bytes(path: Path) -> bytes | None:
    """A file's bytes; None when it does not exist. Other errors raise."""
    try:
        return Path(path).read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None


def _dir_mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _cli_version(path: str) -> str | None:
    """``<path> --version``'s output, or None when it exits non-zero."""
    from ai_quota_swap import spawn

    proc = spawn.run(
        [path, "--version"],
        capture_output=True,
        text=True,
        timeout=VERSION_TIMEOUT_S,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout or proc.stderr


def _launchctl_print() -> tuple[int, str]:
    """``(exit code, stdout)`` of ``launchctl print`` for the menu bar job."""
    from ai_quota_swap import launch_agent, spawn

    proc = spawn.run(
        ["/bin/launchctl", "print", launch_agent.service_target()],
        capture_output=True,
        text=True,
        timeout=LAUNCHCTL_TIMEOUT_S,
        check=False,
    )
    return proc.returncode, proc.stdout or ""


def _keychain_answers() -> None:
    """Raise unless the login Keychain answers a read-only item lookup.

    Looks up an item that never exists: a working Keychain says "not found"
    (exit 44, ``get_password`` returns None) and nothing is decrypted or
    written. A locked, denied or missing Keychain raises.
    """
    from ai_quota_swap import macos_keychain

    macos_keychain.get_password(KEYCHAIN_PROBE_SERVICE, macos_keychain.keychain_account_name())


def _codex_keyring_login(home: Path) -> bool:
    from ai_quota_swap.codex import auth as codex_auth

    return codex_auth._keyring_login_present(home)


def _heartbeat(backup_dir: Path) -> dict | None:
    from ai_quota_swap import autoswitch

    return autoswitch.read_heartbeat(backup_dir)


def _install_paths() -> list[str]:
    import ai_quota_swap

    return [sys.prefix, sys.executable, os.path.dirname(os.path.abspath(ai_quota_swap.__file__))]


def _install_info() -> dict:
    """Python version, install method (``uv``/``pipx``/None), build, editable."""
    from importlib import metadata

    from ai_quota_swap.update_check import _detect_install_method, install_identity

    editable = False
    try:
        raw = metadata.distribution("ai-quota-swap").read_text("direct_url.json")
        if raw:
            editable = bool((json.loads(raw).get("dir_info") or {}).get("editable"))
    except Exception:
        editable = False
    return {
        "python": _platform.python_version(),
        "method": _detect_install_method(),
        "identity": install_identity(),
        "editable": editable,
    }


def _reach(url: str) -> str:
    """``"HTTP 404"``: any HTTP answer means the host is reachable. Raises
    when it is not (no route, DNS, TLS, timeout)."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": "ai-quota-swap doctor"}
    )
    try:
        with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT_S) as resp:
            return f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"


# Thin wrappers so a default looks its target up at call time: a test (or the
# demo harness) that replaces ``Path.home`` or ``paths.get_backup_root`` is
# then honoured here too.


def _platform_now() -> Platform:
    return Platform.detect()


def _environ() -> Mapping[str, str]:
    return os.environ


def _claude_settings_path() -> Path:
    return paths.get_claude_config_home() / "settings.json"


def _list_dir(path: Path) -> list[Path]:
    """A directory's entries; [] when it does not exist."""
    try:
        return sorted(Path(path).iterdir())
    except FileNotFoundError:
        return []


def _claude_sessions() -> list:
    from ai_quota_swap.process_detection import list_sessions

    return list_sessions()


def _claude_unreadable_sessions() -> list:
    from ai_quota_swap.process_detection import unreadable_session_records

    return unreadable_session_records()


def _key_line_answers(command: str) -> str | None:
    """Whether qswap's key line answers: None, or why not (it runs the line's
    command with ``--check``, which prints nothing)."""
    from ai_quota_swap import claude_settings

    return claude_settings.check_command(command)


#: The machine-wide managed settings file, which outranks every other layer
#: (Claude Code's documented precedence), by platform.
MANAGED_SETTINGS = {
    Platform.MACOS: Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    Platform.LINUX: Path("/etc/claude-code/managed-settings.json"),
    Platform.WINDOWS: Path("C:/Program Files/ClaudeCode/managed-settings.json"),
}


def _managed_settings_path() -> Path | None:
    return MANAGED_SETTINGS.get(Platform.detect())


def _cwd() -> Path:
    return Path.cwd()


def _link_leads_to(path: Path) -> str | None:
    """Where the link at ``path`` leads (resolved, a dangling one too), or
    None when ``path`` is not a link."""
    from ai_quota_swap.fsutil import is_link

    if not is_link(path):
        return None
    return os.path.realpath(path)


def _now() -> float:
    return time.time()


def _home() -> Path:
    return Path.home()


def _claude_config_path() -> Path:
    return paths.get_global_config_path()


def _codex_home() -> Path:
    return paths.get_codex_home()


def _data_dir() -> Path:
    return paths.get_backup_root()


def _which(name: str) -> str | None:
    from ai_quota_swap import clis

    return clis.find_cli(name)


def _exists(path: Path) -> bool:
    return os.path.exists(path)


def _is_dir(path: Path) -> bool:
    return os.path.isdir(path)


def _access(path: Path, mode: int) -> bool:
    return os.access(path, mode)


@dataclass(frozen=True)
class Probes:
    """Every way doctor touches the system, as replaceable callables.

    :func:`default_probes` gives the real ones; tests take those and replace
    what they fake (``dataclasses.replace``). A probe may raise anything: the
    checks treat a raise as "could not tell".
    """

    platform: Callable[[], Platform]
    now: Callable[[], float]
    home: Callable[[], Path]
    claude_config_path: Callable[[], Path]
    codex_home: Callable[[], Path]
    data_dir: Callable[[], Path]
    which: Callable[[str], str | None]
    cli_version: Callable[[str], str | None]
    read_bytes: Callable[[Path], bytes | None]
    exists: Callable[[Path], bool]
    is_dir: Callable[[Path], bool]
    dir_mode: Callable[[Path], int]
    access: Callable[[Path, int], bool]
    heartbeat: Callable[[Path], dict | None]
    keychain_answers: Callable[[], None]
    codex_keyring_login: Callable[[Path], bool]
    launchctl_print: Callable[[], tuple[int, str]]
    install_paths: Callable[[], list[str]]
    install_info: Callable[[], dict]
    reach: Callable[[str], str]
    #: The environment qswap runs in (only names are ever reported).
    environ: Callable[[], Mapping[str, str]] = _environ
    #: Claude Code's user settings file (``~/.claude/settings.json``).
    claude_settings_path: Callable[[], Path] = _claude_settings_path
    #: The Claude Code sessions running on the shared login.
    claude_sessions: Callable[[], list] = _claude_sessions
    #: A directory's entries (the run profiles under the data directory).
    list_dir: Callable[[Path], list[Path]] = _list_dir
    #: The session records on the shared login that could not be read.
    claude_unreadable_sessions: Callable[[], list] = _claude_unreadable_sessions
    #: Whether qswap's key line answers: None, or why not.
    key_line_answers: Callable[[str], str | None] = _key_line_answers
    #: Where a link leads (resolved), or None for a path that is not a link.
    link_leads_to: Callable[[Path], str | None] = _link_leads_to
    #: The directory doctor runs in (its project and local settings).
    cwd: Callable[[], Path] = _cwd
    #: The machine-wide managed settings file, or None on this platform.
    managed_settings_path: Callable[[], Path | None] = _managed_settings_path


def default_probes() -> Probes:
    """The real probes, looked up when called (so a module-level patch of,
    say, ``doctor._cli_version`` takes effect)."""
    return Probes(
        platform=_platform_now,
        now=_now,
        home=_home,
        claude_config_path=_claude_config_path,
        codex_home=_codex_home,
        data_dir=_data_dir,
        which=_which,
        cli_version=_cli_version,
        read_bytes=_read_bytes,
        exists=_exists,
        is_dir=_is_dir,
        dir_mode=_dir_mode,
        access=_access,
        heartbeat=_heartbeat,
        keychain_answers=_keychain_answers,
        codex_keyring_login=_codex_keyring_login,
        launchctl_print=_launchctl_print,
        install_paths=_install_paths,
        install_info=_install_info,
        reach=_reach,
        environ=_environ,
        claude_settings_path=_claude_settings_path,
        claude_sessions=_claude_sessions,
        list_dir=_list_dir,
        claude_unreadable_sessions=_claude_unreadable_sessions,
        key_line_answers=_key_line_answers,
        link_leads_to=_link_leads_to,
        cwd=_cwd,
        managed_settings_path=_managed_settings_path,
    )


def _try(fn: Callable, *args, default=None):
    """``(fn(*args), None)``, or ``(default, the exception)``."""
    try:
        return fn(*args), None
    except Exception as e:  # a probe that fails or is refused reads as unknown
        return default, e


def _reason(exc: BaseException | None) -> str:
    """A short, one-line reason for a failed probe."""
    if exc is None:
        return "unknown"
    if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)):
        return "timed out"
    reason = getattr(exc, "reason", None)  # urllib.error.URLError wraps the cause
    if isinstance(reason, TimeoutError):
        return "timed out"
    if isinstance(reason, BaseException):
        text = getattr(reason, "strerror", None) or str(reason)
    elif isinstance(reason, str) and reason:
        text = reason
    else:
        text = getattr(exc, "strerror", None) or str(exc)
    text = (text or "").strip()
    text = text.splitlines()[0] if text else type(exc).__name__
    return text if len(text) <= 48 else text[:47] + "…"


# -- small helpers -------------------------------------------------------------------


def parse_version(output: str | None) -> str | None:
    """The first version number in ``--version`` output (``"2.1.4 (Claude
    Code)"`` → ``"2.1.4"``, ``"codex-cli 0.167.0"`` → ``"0.167.0"``)."""
    if not output:
        return None
    match = _VERSION_RE.search(output)
    return match.group(0) if match else None


def parse_launchctl_print(text: str) -> dict:
    """The job's own fields from ``launchctl print``: ``state``, ``pid``,
    ``last_exit`` (int, or None when it never exited) and ``last_signal``.

    Only lines at a single tab are the job's; deeper ones belong to nested
    objects that repeat the same keys (see ``launch_agent._job_state``).
    """
    out: dict = {"state": None, "pid": None, "last_exit": None, "last_signal": None}
    for line in (text or "").splitlines():
        if not line.startswith("\t") or line.startswith("\t\t"):
            continue
        key, sep, value = line.strip().partition(" = ")
        if not sep:
            continue
        value = value.strip()
        if key == "state" and out["state"] is None:
            out["state"] = value
        elif key == "pid" and out["pid"] is None and value.isdigit():
            out["pid"] = int(value)
        elif key == "last exit code" and out["last_exit"] is None:
            head = value.split(":", 1)[0].strip()
            if head.lstrip("-").isdigit():
                out["last_exit"] = int(head)
        elif key == "last terminating signal" and out["last_signal"] is None:
            out["last_signal"] = value
    return out


def _abbr(path: object, home: Path | None) -> str:
    """``path`` with the home directory spelled ``~``."""
    text = str(path)
    if home is None:
        return text
    base = str(home).rstrip("/\\")
    if base and text == base:
        return "~"
    if base and (text.startswith(base + "/") or text.startswith(base + "\\")):
        return "~" + text[len(base):]
    return text


def _read_json(probes: Probes, path: Path) -> tuple[dict | None, BaseException | None]:
    """``(data, None)``; ``(None, None)`` when absent; ``(None, error)`` when
    unreadable or not a JSON object."""
    raw, err = _try(probes.read_bytes, path)
    if err is not None:
        return None, err
    if raw is None:
        return None, None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        return None, ValueError(f"not valid JSON ({e.__class__.__name__})")
    if not isinstance(data, dict):
        return None, ValueError("not a JSON object")
    return data, None


def _accounts(data: dict | None) -> dict:
    accounts = (data or {}).get("accounts")
    return accounts if isinstance(accounts, dict) else {}


def claude_slot(data: dict | None, email: str, organization_uuid: str) -> str | None:
    """The managed number for a Claude login: same ``(email, organizationUuid)``.

    The rule of ``ClaudeAccountSwitcher._find_account_slot``, kept here so
    doctor need not import the switcher; a test holds the two together.
    """
    for num, account in _accounts(data).items():
        if not isinstance(account, dict):
            continue
        if (account.get("email") == email
                and account.get("organizationUuid", "") == organization_uuid):
            return str(num)
    return None


def codex_slot(data: dict | None, user_id: str, account_id: str, email: str) -> str | None:
    """The managed number for a Codex login: same ``(uuid, organizationUuid)``,
    or ``(email, organizationUuid)`` for a record saved without a uuid.

    The rule of ``CodexSwitcher._slot_for_identity`` (a test holds the two
    together).
    """
    for num, record in _accounts(data).items():
        if not isinstance(record, dict):
            continue
        org = record.get("organizationUuid", "") or ""
        uuid = (record.get("uuid") or "").strip()
        if org != account_id:
            continue
        if uuid and uuid == user_id:
            return str(num)
        if not uuid and record.get("email") == email:
            return str(num)
    return None


def _label(num: str, record: dict, email: str) -> str:
    from ai_quota_swap.labels import account_label

    alias = record.get("alias") if isinstance(record.get("alias"), str) else None
    return account_label(num, alias=alias or None, email=email)


def under_protected_folder(path: str, home: Path) -> str | None:
    """``"Documents"`` when ``path`` lies in ``~/Documents`` (likewise Desktop,
    Downloads), else None. Compares both the given and the resolved spelling,
    case-insensitively like the default macOS volume."""

    def spellings(p: str) -> set[str]:
        out = {os.path.normpath(p)}
        try:
            out.add(os.path.realpath(p))
        except (OSError, ValueError):
            pass
        return {s.casefold() for s in out}

    candidates = spellings(path)
    for folder in PROTECTED_FOLDERS:
        for root in spellings(str(Path(home) / folder)):
            for cand in candidates:
                if cand == root or cand.startswith(root.rstrip(os.sep) + os.sep):
                    return folder
    return None


# -- the checks, by group -------------------------------------------------------------


@dataclass
class _Facts:
    """What several groups need, computed once."""

    platform: Platform
    home: Path | None
    data_dir: Path | None
    codex_home: Path | None
    claude_bin: str | None
    codex_bin: str | None
    codex_present: bool = False
    codex_registry: dict | None = None
    codex_registry_error: BaseException | None = None
    menubar_configured: bool = False
    menubar_login: bool = False  # the plist sits in ~/Library/LaunchAgents
    menubar_plist: Path | None = None
    #: The key active through qswap's line, and the switch that leaves it
    #: (set by the key-line checks).
    key_line_active: str | None = None
    key_line_leave: str = "qswap switch N"


def _claude_checks(p: Probes, f: _Facts) -> list[Check]:
    from ai_quota_swap.display import provider_name

    name = provider_name("claude")
    checks: list[Check] = []
    if f.claude_bin:
        raw, _ = _try(p.cli_version, f.claude_bin)
        version = parse_version(raw)
        summary = f"{name} {version} on PATH" if version else f"{name} on PATH, version unknown"
        checks.append(Check("claude.cli", "claude", PASS, summary))
    else:
        install = CLAUDE_INSTALL_WINDOWS if f.platform == Platform.WINDOWS else CLAUDE_INSTALL
        # Codex alone is a working setup, so a missing Claude Code then only warns.
        checks.append(Check(
            "claude.cli", "claude", WARN if f.codex_bin else FAIL,
            f"{name} was not found (claude is not on PATH)", fix=install,
        ))

    config_path, err = _try(p.claude_config_path)
    data, read_err = (None, err) if err is not None else _read_json(p, config_path)
    if read_err is not None:
        where = _abbr(config_path, f.home) if config_path is not None else "its config"
        checks.append(Check(
            "claude.login", "claude", WARN,
            f"could not read {where} ({_reason(read_err)})",
            fix="claude, then /login",
        ))
        return checks
    oauth = (data or {}).get("oauthAccount")
    oauth = oauth if isinstance(oauth, dict) else {}
    email = oauth.get("emailAddress") if isinstance(oauth.get("emailAddress"), str) else ""
    if not email:
        checks.append(Check(
            "claude.login", "claude", WARN, f"{name} is not logged in",
            fix="claude, then /login, then qswap add",
        ))
        return checks
    checks.append(Check("claude.login", "claude", PASS, f"logged in as {email}"))

    org = oauth.get("organizationUuid", "") or ""
    registry, reg_err = (None, None)
    if f.data_dir is not None:
        registry, reg_err = _read_json(p, f.data_dir / "sequence.json")
    if reg_err is not None:
        seq = _abbr(f.data_dir / "sequence.json", f.home)
        checks.append(Check(
            "claude.managed", "claude", WARN,
            f"could not read the account list ({_reason(reg_err)})",
            fix=f"repair or move {seq}, then run qswap list",
        ))
        return checks
    slot = claude_slot(registry, email, org)
    if slot is None:
        checks.append(Check(
            "claude.managed", "claude", WARN, "that login is not managed by qswap",
            fix="qswap add",
        ))
    else:
        record = _accounts(registry).get(slot) or {}
        checks.append(Check(
            "claude.managed", "claude", PASS,
            f"managed by qswap as {_label(slot, record, email)}",
        ))
    return checks


def _claude_override_checks(p: Probes, f: _Facts) -> list[Check]:
    """What makes Claude Code ignore the active account, or what the active
    account does to running sessions (docs/design/api-key-sessions.md). Each
    appears only when it applies."""
    from ai_quota_swap import display

    checks: list[Check] = []
    checks.extend(_key_helper_checks(p, f))
    checks.extend(_other_settings_checks(p, f))

    environ, _ = _try(p.environ, default={})
    names = display.exported_overrides(environ or {})
    following = []
    if f.key_line_active is not None and display.OAUTH_TOKEN_VAR in names:
        # The key line comes before this variable (decision B6, F62).
        names = [n for n in names if n != display.OAUTH_TOKEN_VAR]
        following = [display.OAUTH_TOKEN_VAR]
    if names:
        verb = "is" if len(names) == 1 else "are"
        checks.append(Check(
            "claude.env", "claude", WARN,
            f"{' and '.join(names)} {verb} set in this shell; Claude Code started "
            "from it uses that instead of the active account",
            fix=f"unset {' '.join(names)}",
        ))
    elif following:
        checks.append(Check(
            "claude.env", "claude", WARN,
            f"{display.OAUTH_TOKEN_VAR} is set in this shell, but the key line comes "
            f"before it: Claude Code started from it uses {f.key_line_active} too",
            fix=f"to keep those sessions off the key, leave it: {f.key_line_leave}",
        ))

    stale = _api_key_session_check(p, f)
    if stale is not None:
        checks.append(stale)
    records = _session_records_check(p, f)
    if records is not None:
        checks.append(records)
    return checks


def _key_helper_checks(p: Probes, f: _Facts) -> list[Check]:
    """Claude Code's user settings and qswap's key helper line
    (docs/design/api-key-sessions.md, part 2), compared with the account
    list, key_helper.json, the run profiles and ``activeAccountNumber`` by
    the same function every switch and surface uses
    (``key_activation.assess_key_state``, decision B5): qswap's own line for
    an API-key account is the active key, reported as such; a line for an
    account that does not exist (or is not an API-key account) fails with
    the repair; another helper overrides the active account; a line that
    came back, a record whose line is gone, profiles still linked under the
    line or left with a copy after it, and a settings file that cannot be
    read while qswap's record names a key are each reported."""
    from ai_quota_swap import claude_settings, display
    from ai_quota_swap.key_activation import KEY_STATE_FILENAME, assess_key_state

    checks: list[Check] = []
    settings_path, err = _try(p.claude_settings_path)
    if err is not None or settings_path is None:
        return checks
    where = _abbr(settings_path, f.home)
    raw, read_err = _try(p.read_bytes, settings_path)
    registry, reg_err = None, None
    record = None
    if f.data_dir is not None:
        registry, reg_err = _read_json(p, f.data_dir / "sequence.json")
        record, _ = _read_json(p, f.data_dir / KEY_STATE_FILENAME)
    fix = _key_repair_command(p, f, registry)
    if read_err is None:
        try:
            text = None if raw is None else raw.decode("utf-8")
        except UnicodeDecodeError:
            text = "\x00"
        state = claude_settings.state_of_text(
            text, Path(settings_path), data_dir=f.data_dir, default_dir=_default_data_dir(f),
        )
    else:
        state = claude_settings.HelperState(
            claude_settings.INVALID, Path(settings_path),
            error=f"could not be read: {_reason(read_err)}",
        )
    accounts = None if reg_err is not None else _accounts(registry)
    login_slot = fix.removeprefix("qswap switch ") if fix != "qswap switch N" else None
    findings = assess_key_state(
        state, accounts, record, active_number=(registry or {}).get("activeAccountNumber"),
        login_slot=login_slot,
        linked=tuple(_profiles_linked(p, f, settings_path)) if state.ours else (),
        copies=tuple(range(_stale_profile_copies(p, f, state))),
    )

    def label(number: str) -> str:
        row = _accounts(registry).get(number)
        row = row if isinstance(row, dict) else {}
        return _label(number, row, row.get("email", "") if isinstance(row.get("email"), str) else "")

    env_check = _settings_env_check(
        raw if read_err is None else None, where, findings.key,
        label(findings.key) if findings.key is not None else None,
    )
    if state.kind == claude_settings.INVALID:
        if read_err is None:
            checks.append(Check(
                "claude.settings", "claude", WARN,
                f"{where} is not valid JSON ({state.error}); Claude Code cannot read "
                "it and qswap will not change it",
                fix=f"fix the JSON in {where}",
            ))
        if findings.unreadable_record is not None:
            sentence = " ".join(display.key_settings_unreadable(
                where, state.error, label(findings.unreadable_record)
            ))
            step = display.settings_fix(where, state.error)
            checks.append(Check(
                "claude.key_helper", "claude", FAIL,
                sentence.removesuffix(f" {step}, then retry.").rstrip("."),
                fix=step[:1].lower() + step[1:],
            ))
    elif state.kind == claude_settings.FOREIGN:
        checks.append(Check(
            "claude.api_key_helper", "claude", WARN,
            f"{where} sets apiKeyHelper; Claude Code uses it instead of the "
            "active account, and qswap cannot add its own",
            fix=f"remove apiKeyHelper from {where}",
        ))
    elif state.kind == claude_settings.OTHER:
        # Decision SS4: another qswap's line, for its own data directory
        # (named on a line of its own): not this qswap's to check or repair.
        checks.append(Check(
            "claude.api_key_helper", "claude", WARN,
            # At most one apostrophe: the printer takes the text between two
            # for one quoted word, which it never wraps.
            f"{where} holds the key line of another qswap; Claude Code uses the key "
            "of that qswap instead of the active account, and this qswap cannot add "
            "its own line. Its data directory:",
            fix=f"remove apiKeyHelper from {where}",
            path=_abbr(state.data_dir, f.home),
        ))
    elif state.kind == claude_settings.OURS:
        row = _accounts(registry).get(state.number)
        # One name per account (decision DS7): its label where it exists.
        named = label(state.number) if isinstance(row, dict) else f"#{state.number}"
        if findings.problem is not None:
            what = "does not exist" if not isinstance(row, dict) else (
                "is not an API-key account"
            )
            checks.append(Check(
                "claude.key_helper", "claude", FAIL,
                f"{where} makes {named} the active API key, but {named} "
                f"{what}; new Claude Code sessions fail to get a key, and "
                f"{display.BROKEN_LINE_RUNNING}",
                fix=fix,
            ))
        elif state.stale_command:
            checks.append(Check(
                "claude.key_helper", "claude", FAIL,
                f"{where} makes {named} the active API key with a qswap that "
                "is no longer there; new Claude Code sessions fail to get the key, "
                f"and {display.BROKEN_LINE_RUNNING}",
                fix=f"qswap switch {state.number}",
            ))
        elif findings.key is not None:
            f.key_line_active = label(findings.key)
            f.key_line_leave = fix
            watch = _engine_watching(p, f)
            if findings.entered_by == "engine" and not watch:
                # Decisions B8 (F33), DS3, DS4: nothing leaves the key now.
                from ai_quota_swap.autoswitch import key_watch_words

                checks.append(Check(
                    "claude.key_watch", "claude", WARN,
                    key_watch_words(
                        label(findings.key), watch, _now(p),
                        also=" and sessions stay on the key",
                    ),
                    fix=_key_way_back(p, registry, login_slot) or display.KEY_NO_WAY_BACK,
                ))
            check = _key_line_check(p, f, state, row if isinstance(row, dict) else {}, where)
            if findings.came_back and check.status == PASS:
                check = Check(
                    "claude.key_helper", "claude", WARN,
                    f"{where} makes {label(state.number)} the active API key again: "
                    "its key line came back after qswap took it out (another "
                    "program wrote an older copy of the settings)",
                    fix=fix,
                )
            if env_check is not None and check.status == PASS:
                # The env block is about this key: the key check carries
                # its warning, rather than passing beside it (as the note
                # does in list and status).
                check = Check(
                    "claude.key_helper", "claude", WARN,
                    f"{check.summary}. {env_check.summary}", fix=env_check.fix,
                )
                env_check = None
            checks.append(check)
    if env_check is not None:
        checks.append(env_check)
    lost_said = False
    if findings.lost is not None and isinstance((accounts or {}).get(findings.lost), dict):
        # Decision E1: one warning for the record and activeAccountNumber
        # together. Its fix is the way off the key (the switch to the
        # account underneath, which clears both); using the key again is
        # offered second, and labelled as that.
        lost_said = True
        also = (
            ", and the account list names it as the active account"
            if findings.stale_active == findings.lost else ""
        )
        checks.append(Check(
            "claude.key_helper", "claude", WARN,
            f"qswap made {label(findings.lost)} the active API key{also}, but its key "
            f"line is no longer in {where}; Claude Code sessions do not use it",
            fix=_key_way_back(p, registry, login_slot) or display.KEY_NO_WAY_BACK,
            hint=f"to use the key again: qswap switch {findings.lost}",
        ))
    if findings.stale_active is not None and not (
        lost_said and findings.stale_active == findings.lost
    ):
        active_fix = fix
        if fix == "qswap switch N":
            # No login underneath to name: the way off a key.
            active_fix = _key_way_back(p, registry, None) or display.KEY_NO_WAY_BACK
        checks.append(Check(
            "claude.active_number", "claude", WARN,
            f"the account list names {label(findings.stale_active)} as the active "
            "account, but its key line is gone; the next switch sets it back",
            fix=active_fix,
        ))
    if findings.linked:
        n = len(findings.linked)
        noun = "profile still shares" if n == 1 else "profiles still share"
        its = "its" if n == 1 else "their"
        checks.append(Check(
            "claude.profile_settings", "claude", FAIL,
            f"{n} qswap run {noun} Claude Code's settings while the key line for "
            f"{label(findings.key)} is in them, so {its} sessions use the key too",
            fix=f"qswap switch {findings.key}",
        ))
    stale = len(findings.copies)
    if stale:
        noun = "profile keeps" if stale == 1 else "profiles keep"
        checks.append(Check(
            "claude.profile_settings", "claude", FAIL,
            f"{stale} qswap run {noun} a copy of Claude Code's settings from while a "
            "key was active, so changes to the settings do not reach them",
            fix=fix,
        ))
    return checks


def _now(p: Probes) -> float:
    now, _ = _try(p.now, default=None)
    return now if isinstance(now, (int, float)) else time.time()


def _engine_watching(p: Probes, f: _Facts):
    """Whether an auto-switch engine for Claude Code watches a key's limit
    (``autoswitch.key_watch``: a loop that is not a dry run, or a ``--once``
    tick in the last 15 minutes); truthy when one does."""
    from ai_quota_swap import autoswitch

    if f.data_dir is None:
        return autoswitch.KeyWatch()
    beat, _ = _try(p.heartbeat, f.data_dir)
    return autoswitch.key_watch(beat, _now(p))


def _defeating(raw: bytes | None) -> tuple[bool, list[str], str | None]:
    """What a settings file sets that outranks or redirects qswap's line:
    ``(apiKeyHelper set, env variables by name, the other host)``."""
    from ai_quota_swap import key_activation

    try:
        text = raw.decode("utf-8") if raw else None
    except UnicodeDecodeError:
        text = None
    obj = key_activation.settings_object(text)
    env = key_activation.env_block(obj)
    return (
        "apiKeyHelper" in obj, key_activation.defeating_names(env),
        key_activation.other_host(env),
    )


def _settings_env_check(
    raw: bytes | None, where: str, key: str | None, named: str | None = None,
) -> Check | None:
    """The user settings' env block (decision B6, F59): a variable Claude
    Code obeys before the key line, or a base URL the key is sent to. Over
    the active key ``key`` (``named`` its label) it says what list and status
    say: what the variable does to that key, the host included (SS9)."""
    from ai_quota_swap.display import join_names, key_line_outranked_consequence

    _helper, names, host = _defeating(raw)
    if names and key is not None:
        return Check(
            "claude.settings_env", "claude", WARN,
            f"{where} sets {join_names(names)} in its env block, which Claude "
            "Code obeys before any key line, "
            + key_line_outranked_consequence(names, named or f"#{key}", host),
            fix=f"remove {join_names(names)} from the env block of {where}, then: "
            f"qswap switch {key}",
        )
    if names:
        return Check(
            "claude.settings_env", "claude", WARN,
            f"{where} sets {join_names(names)} in its env block, which Claude "
            "Code obeys before qswap's key line; qswap will not switch to an API "
            "key while it is there",
            fix=f"remove {join_names(names)} from the env block of {where}",
        )
    if host is not None:
        sent = "the active key" if key is not None else "an API key qswap makes active"
        return Check(
            "claude.settings_env", "claude", WARN,
            f"{where} sets ANTHROPIC_BASE_URL, so Claude Code sends {sent} to {host}",
            fix=f"remove ANTHROPIC_BASE_URL from the env block of {where} if that is not meant",
        )
    return None


def _other_settings_checks(p: Probes, f: _Facts) -> list[Check]:
    """The layers that outrank the user settings (Claude Code's documented
    precedence: managed, then project local, then shared project): one that
    sets apiKeyHelper, a variable that outranks the line, or a base URL
    decides for the sessions that read it, whatever qswap's line says
    (decision B6, F15). Read: the machine's managed file, and the project and
    local settings of the directory doctor runs in."""
    from ai_quota_swap.display import join_names

    checks: list[Check] = []
    user, _ = _try(p.claude_settings_path)
    try:
        user_real = os.path.realpath(user) if user is not None else None
    except (OSError, ValueError):
        user_real = None
    places: list[tuple[str, Path]] = []
    managed, _ = _try(p.managed_settings_path)
    if managed is not None:
        places.append(("managed", Path(managed)))
    cwd, err = _try(p.cwd)
    if err is None and cwd is not None:
        places.append(("project", Path(cwd) / ".claude" / "settings.json"))
        places.append(("local", Path(cwd) / ".claude" / "settings.local.json"))
    # The layer in words; its path goes on a line of its own (Check.path).
    # (One apostrophe at most in the sentence: two would read as a quoted
    # argument to the wrapper, which never breaks one.)
    layer = {
        "managed": "The managed settings of this machine",
        "project": "The shared settings of this project",
        "local": "The local settings of this project",
    }
    for kind, path in places:
        try:
            if user_real is not None and os.path.realpath(path) == user_real:
                continue  # doctor runs in the home directory: that is the user file
        except (OSError, ValueError):
            pass
        raw, read_err = _try(p.read_bytes, path)
        if read_err is not None or not raw:
            continue
        helper, names, host = _defeating(raw)
        what = (["apiKeyHelper"] if helper else []) + names + (
            ["ANTHROPIC_BASE_URL"] if host else []
        )
        if not what:
            continue
        where = _abbr(path, f.home)
        fix = (
            "ask whoever manages this machine about the file above" if kind == "managed"
            else f"remove {join_names(what)} from the file above"
        )
        checks.append(Check(
            "claude.other_settings", "claude", WARN,
            f"{layer[kind]} set {join_names(what)}; Claude Code reads them before "
            "your user settings, so sessions that read them do not follow qswap's "
            "key line:",
            fix=fix, path=where,
        ))
    return checks


def _profiles_linked(p: Probes, f: _Facts, settings_path: Path) -> list[Path]:
    """Run profiles whose ``settings.json`` is a link leading to the user
    settings (F9): while the key line is in, their sessions follow it."""
    if f.data_dir is None:
        return []
    target, err = _try(os.path.realpath, settings_path)
    if err is not None:
        return []
    entries, _ = _try(p.list_dir, f.data_dir / "sessions", default=[])
    out = []
    for entry in entries or []:
        if Path(entry).name.startswith("."):
            continue
        leads, _ = _try(p.link_leads_to, Path(entry) / "settings.json")
        if leads is not None and os.path.normcase(leads) == os.path.normcase(target):
            out.append(Path(entry))
    return out


def _key_line_check(p: Probes, f: _Facts, state, record: dict, where: str) -> Check:
    """qswap's line for an API-key account: run with ``--check`` the way
    Claude Code runs it (decision B2). It passes naming the command it runs,
    or fails with why it cannot answer and the next step: the helper's own
    when its message names one, else ``qswap switch N``, which writes the
    line again for this install. A check that could not be made warns."""
    from ai_quota_swap import claude_settings

    email = record.get("email") if isinstance(record.get("email"), str) else ""
    named = _label(state.number, record, email)  # one name per account (DS7)
    why, err = _try(p.key_line_answers, state.command)
    if err is not None:
        return Check(
            "claude.key_helper", "claude", WARN,
            f"{where} makes {named} the active API key; qswap could not "
            f"check that its line answers ({_reason(err)})",
            fix=f"qswap key-helper {state.number} --check",
        )
    if why is not None:
        line = claude_settings.read_line(state.command)
        then = ""
        if line is None or line.module != "-m":
            then = (
                "; new Claude Code sessions fail to get a key from it, and running "
                "ones keep the key they have until the settings change"
            )
        # The helper names its own fix when it has one ("save one: qswap
        # add-token KEY --slot 5"); else the switch writes the line again.
        step = re.search(r": (qswap [^;()]+)\)$", why)
        return Check(
            "claude.key_helper", "claude", FAIL,
            f"{where} makes {named} the active API key, but its line {why}{then}",
            fix=step.group(1) if step else f"qswap switch {state.number}",
        )
    return Check(
        "claude.key_helper", "claude", PASS,
        f"active API key: {named}, through "
        f"qswap's key line in {where}, which runs "
        f"{_line_program(state.command, f.home)}",
    )


def _line_program(command: str | None, home: Path | None) -> str:
    """What qswap's line runs, for a message: the program (``~`` for the home
    directory, in quotes when it has a space) and, for an interpreter, its
    ``-m`` flags."""
    from ai_quota_swap import claude_settings

    line = claude_settings.read_line(command)
    if line is None:
        return "an unknown command"
    shown = _abbr(line.program, home)
    if any(c.isspace() for c in shown):
        shown = f'"{shown}"'
    return f"{shown} {line.module} ai_quota_swap" if line.module else shown


def _default_data_dir(f: _Facts) -> Path | None:
    """The data directory a key line without ``--data-dir`` belongs to on
    this machine: on Linux this qswap's own (decision R4-4: every line
    qswap writes there names it), ``~/.ai-quota-swap`` elsewhere (decision
    SS4)."""
    if f.platform in (Platform.LINUX, Platform.WSL):
        return f.data_dir
    if f.home is None:
        return None
    return f.home / paths.LEGACY_BACKUP_DIRNAME


def _key_repair_command(p: Probes, f: _Facts, registry: dict | None) -> str:
    """``qswap switch N`` for the live login, which removes a broken key line
    and restores run profiles' settings sharing."""
    config_path, err = _try(p.claude_config_path)
    if err is None:
        data, _ = _read_json(p, config_path)
        oauth = (data or {}).get("oauthAccount")
        if isinstance(oauth, dict) and isinstance(oauth.get("emailAddress"), str):
            slot = claude_slot(registry, oauth["emailAddress"], oauth.get("organizationUuid", "") or "")
            if slot is not None:
                return f"qswap switch {slot}"
    return "qswap switch N"


def _key_way_back(p: Probes, registry: dict | None, login_slot: str | None) -> str | None:
    """The way off the active key, as ``key_way_back`` gives it to status
    and the stopping engine: the switch to the login underneath; ``qswap
    add`` over a login qswap does not manage; else the switch to the first
    subscription. None when there is no subscription."""
    accounts = _accounts(registry)

    def kind(number: str) -> object:
        row = accounts.get(str(number))
        return row.get("kind") if isinstance(row, dict) else None

    if login_slot is not None and kind(login_slot) != "api_key":
        return f"qswap switch {login_slot}"
    if login_slot is None:
        config_path, err = _try(p.claude_config_path)
        if err is None:
            data, _ = _read_json(p, config_path)
            oauth = (data or {}).get("oauthAccount")
            if isinstance(oauth, dict) and oauth.get("emailAddress"):
                return "qswap add"
    sequence = (registry or {}).get("sequence") if isinstance(registry, dict) else None
    for number in sequence if isinstance(sequence, list) else []:
        row = accounts.get(str(number))
        if isinstance(row, dict) and row.get("kind") != "api_key" and not row.get("disabled"):
            return f"qswap switch {number}"
    return None


def _stale_profile_copies(p: Probes, f: _Facts, state) -> int:
    """How many run profiles hold a settings copy (session.SETTINGS_COPY_MARKER)
    while the user settings carry no key line of qswap's."""
    from ai_quota_swap import claude_settings
    from ai_quota_swap.session import SETTINGS_COPY_MARKER

    if f.data_dir is None or (state is not None and state.ours):
        return 0
    if state is not None and state.kind == claude_settings.INVALID:
        return 0  # cannot tell whether the line is in: nothing to report
    entries, err = _try(p.list_dir, f.data_dir / "sessions", default=[])
    count = 0
    for entry in entries or []:
        if Path(entry).name.startswith("."):
            continue
        found, _ = _try(p.exists, Path(entry) / SETTINGS_COPY_MARKER, default=False)
        if found:
            count += 1
    return count


def _session_records_check(p: Probes, f: _Facts) -> Check | None:
    """Claude Code session records qswap cannot read. While one is there
    qswap cannot tell whether sessions are running, so where the key helper
    cannot be used it refuses every switch to an API-key account. Claude Code
    names each record after its process id, so a record whose process is gone
    can be deleted."""
    records, _ = _try(p.claude_unreadable_sessions, default=[])
    paths = [str(r) for r in records or []]
    if not paths:
        return None
    shown = [_abbr(path, f.home) for path in paths]
    files = [s for s, path in zip(shown, paths) if path.lower().endswith(".json")]
    if not files:
        return Check(
            "claude.session_records", "claude", WARN,
            f"Claude Code's session records folder {shown[0]} could not be read, so "
            "qswap cannot tell whether sessions are running and will not switch to "
            "an API-key account without its key line",
            fix=f"check that you can open {shown[0]}",
        )
    n = len(files)
    noun = "record" if n == 1 else "records"
    stem = files[0].replace("\\", "/").rsplit("/", 1)[-1][: -len(".json")]
    if n == 1 and stem.isascii() and stem.isdigit():
        fix = f"if Claude Code process {stem} is gone, delete {files[0]}"
    else:
        fix = (
            "each is named after its Claude Code process; if that process is gone, "
            "delete the file"
        )
    return Check(
        "claude.session_records", "claude", WARN,
        f"{n} Claude Code session {noun} could not be read, so qswap cannot tell "
        "whether sessions are running and will not switch to an API-key account "
        "without its key line: " + ", ".join(files),
        fix=fix,
    )


def _api_key_session_check(p: Probes, f: _Facts) -> Check | None:
    """An API-key account is active while sessions that started on a
    subscription are running: those cannot use the key and are logged out.
    A session that started while another API key was active keeps using
    that key and is not counted."""
    if f.data_dir is None:
        return None
    config_path, err = _try(p.claude_config_path)
    if err is not None:
        return None
    data, _ = _read_json(p, config_path)
    oauth = (data or {}).get("oauthAccount")
    if not isinstance(oauth, dict) or not isinstance(oauth.get("emailAddress"), str):
        return None
    email, org = oauth["emailAddress"], oauth.get("organizationUuid", "") or ""
    registry, _ = _read_json(p, f.data_dir / "sequence.json")
    slot = claude_slot(registry, email, org)
    record = _accounts(registry).get(slot) if slot else None
    if not isinstance(record, dict) or record.get("kind") != "api_key":
        return None
    rows = _activations(p, f.data_dir)
    since = _active_since(rows, email, org)
    if since is None:
        return None

    def is_key(row_email: str, row_org: str) -> bool:
        row_slot = claude_slot(registry, row_email, row_org)
        row = _accounts(registry).get(row_slot) if row_slot else None
        return isinstance(row, dict) and row.get("kind") == "api_key"

    sessions, _ = _try(p.claude_sessions, default=[])
    older = []
    for session in sessions or []:
        started = (getattr(session, "started_at", 0) or 0) / 1000.0
        if not 0 < started < since:
            continue
        # The account active when it started; unknown (before the log)
        # counts as a subscription, the safe reading for a warning.
        at = next((r for r in reversed(rows) if r[0] <= started), None)
        if at is not None and is_key(at[1], at[2]):
            continue
        older.append(session)
    if not older:
        return None
    previous = next(
        (str(r[3]) for r in reversed(rows)
         if r[0] < since and isinstance(r[3], int) and not is_key(r[1], r[2])),
        None,
    )
    n = len(older)
    noun = "session" if n == 1 else "sessions"
    are, they = ("is", "it is") if n == 1 else ("are", "they are")
    back = f"qswap switch {previous}" if previous else "qswap switch N"
    return Check(
        "claude.api_key_sessions", "claude", WARN,
        f"{_label(slot, record, email)} (API key) is active and {n} Claude Code "
        f"{noun} that started on a subscription {are} running; a running "
        f"session cannot use a key, so {they} logged out",
        fix=f"restart them to use the key, or switch back: {back}",
    )


def _activations(p: Probes, data_dir: Path) -> list[tuple[float, str, str, object]]:
    """The activation log as ``(ts, email, org, number)`` rows, oldest first;
    ``[]`` when it cannot be read."""
    from ai_quota_swap.ledger.activations import FILENAME, parse_ts

    raw, err = _try(p.read_bytes, data_dir / FILENAME)
    if err is not None or not raw:
        return []
    rows = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("email"), str):
            ts = parse_ts(obj.get("ts"))
            if ts is not None:
                rows.append((ts, obj["email"], obj.get("org") or "", obj.get("number")))
    rows.sort(key=lambda r: r[0])
    return rows


def _active_since(rows, email: str, org: str) -> float | None:
    """When the account ``(email, org)`` became active: the start of the
    log's last run of rows naming it; None when the last row is another."""
    since = None
    for ts, row_email, row_org, _number in reversed(rows):
        if (row_email, row_org) != (email, org):
            break
        since = ts
    return since


def _codex_store_mode(p: Probes, config: Path) -> tuple[str, BaseException | None]:
    """``cli_auth_credentials_store`` from ``config.toml``; ``"file"`` when unset."""
    raw, err = _try(p.read_bytes, config)
    if err is not None:
        return "file", err
    if raw is None:
        return "file", None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        return "file", ValueError(f"not valid TOML ({e.__class__.__name__})")
    value = data.get("cli_auth_credentials_store", "file")
    return (value if isinstance(value, str) else repr(value)), None


def _codex_checks(p: Probes, f: _Facts) -> list[Check]:
    from ai_quota_swap.codex import auth as codex_auth
    from ai_quota_swap.display import provider_name

    name = provider_name("codex")
    if not f.codex_present:
        return [Check("codex.cli", "codex", SKIP, f"{name} was not found; it is optional")]

    checks: list[Check] = []
    if f.codex_bin:
        raw, _ = _try(p.cli_version, f.codex_bin)
        version = parse_version(raw)
        summary = f"{name} {version} on PATH" if version else f"{name} on PATH, version unknown"
        checks.append(Check("codex.cli", "codex", PASS, summary))
    else:
        checks.append(Check(
            "codex.cli", "codex", WARN, f"{name} was not found (codex is not on PATH)",
            fix=CODEX_INSTALL,
        ))

    home = f.codex_home
    if home is None:
        return checks
    config = home / codex_auth.CONFIG_FILENAME
    shown = _abbr(config, f.home)
    to_file = f'set cli_auth_credentials_store = "file" in {shown}, then codex login'
    mode, err = _codex_store_mode(p, config)
    if err is not None:
        checks.append(Check(
            "codex.storage", "codex", WARN, f"could not read {shown} ({_reason(err)})",
            fix=f"repair {shown}",
        ))
    elif mode in ("keyring", "auto"):
        checks.append(Check(
            "codex.storage", "codex", FAIL,
            f'{name} keeps its login in the OS keyring (cli_auth_credentials_store = '
            f'"{mode}"); qswap supports file storage only',
            fix=to_file,
        ))
        return checks
    elif mode == "ephemeral":
        checks.append(Check(
            "codex.storage", "codex", FAIL,
            f'{name} keeps its login in memory only (cli_auth_credentials_store = '
            f'"ephemeral"); there is no login file for qswap to manage',
            fix=to_file,
        ))
        return checks
    elif mode != "file":
        checks.append(Check(
            "codex.storage", "codex", FAIL,
            f"unrecognized cli_auth_credentials_store = {mode}; qswap supports "
            "file storage only",
            fix=to_file,
        ))
        return checks

    auth_file = home / codex_auth.AUTH_FILENAME
    raw, read_err = _try(p.read_bytes, auth_file)
    if err is None and read_err is None and raw is None:
        in_keyring, _ = _try(p.codex_keyring_login, home, default=False)
        if in_keyring:
            checks.append(Check(
                "codex.storage", "codex", FAIL,
                f"{name}'s login is in the OS keyring (no auth.json, but a keyring "
                "entry exists); qswap supports file storage only",
                fix="codex login",
            ))
            return checks
    if err is None:
        checks.append(Check(
            "codex.storage", "codex", PASS, f"{name} keeps its login in auth.json (file storage)",
        ))

    relogin = "codex login, then qswap codex add"
    if read_err is not None:
        checks.append(Check(
            "codex.login", "codex", WARN,
            f"could not read {_abbr(auth_file, f.home)} ({_reason(read_err)})",
            fix=relogin,
        ))
        return checks
    if raw is None:
        checks.append(Check(
            "codex.login", "codex", WARN, f"{name} is not logged in", fix=relogin,
        ))
        return checks
    try:
        login = codex_auth.parse_login(raw)
    except Exception as e:  # CodexLoginError: an API-key login, a broken file
        checks.append(Check("codex.login", "codex", WARN, str(e), fix=relogin))
        return checks
    checks.append(Check("codex.login", "codex", PASS, f"logged in as {login.email}"))

    if f.codex_registry_error is not None:
        seq = _abbr(paths.get_codex_data_dir(f.data_dir) / "sequence.json", f.home)
        checks.append(Check(
            "codex.managed", "codex", WARN,
            f"could not read the Codex account list ({_reason(f.codex_registry_error)})",
            fix=f"repair or move {seq}, then run qswap codex list",
        ))
        return checks
    slot = codex_slot(f.codex_registry, login.user_id, login.account_id, login.email)
    if slot is None:
        checks.append(Check(
            "codex.managed", "codex", WARN, "that login is not managed by qswap",
            fix="qswap codex add",
        ))
    else:
        record = _accounts(f.codex_registry).get(slot) or {}
        checks.append(Check(
            "codex.managed", "codex", PASS,
            f"managed by qswap as {_label(slot, record, login.email)}",
        ))
    return checks


def _data_checks(p: Probes, f: _Facts) -> list[Check]:
    if f.data_dir is None:
        return [Check("data.dir", "data", WARN, "could not work out the data directory",
                      fix="check HOME and XDG_DATA_HOME")]
    dd = f.data_dir
    shown = _abbr(dd, f.home)
    checks: list[Check] = []
    exists, _ = _try(p.exists, dd, default=False)
    is_dir, _ = _try(p.is_dir, dd, default=False)
    if exists and not is_dir:
        return [Check("data.dir", "data", FAIL, f"{shown} is not a directory",
                      fix=f"move {shown} aside")]
    if not exists:
        checks.append(Check("data.dir", "data", PASS, f"{shown} (not created yet)",
                            hint="qswap add creates it"))
    else:
        checks.append(Check("data.dir", "data", PASS, shown))
        if f.platform != Platform.WINDOWS:
            mode, _ = _try(p.dir_mode, dd)
            if isinstance(mode, int):
                if mode & 0o077:
                    checks.append(Check(
                        "data.permissions", "data", WARN,
                        f"other users can open it (mode {mode:o})",
                        fix=f"chmod 700 {shown}",
                    ))
                else:
                    checks.append(Check(
                        "data.permissions", "data", PASS, f"private to you (mode {mode:o})",
                    ))

    if f.platform == Platform.MACOS:
        _, err = _try(p.keychain_answers)
        if err is None:
            checks.append(Check(
                "data.store", "data", PASS,
                "saved logins are kept in the login Keychain, which is responding",
            ))
        else:
            rc = re.search(r"rc=(-?\d+)", str(err))  # KeychainError names the exit code
            why = f"security exited {rc.group(1)}" if rc else _reason(err)
            checks.append(Check(
                "data.store", "data", WARN,
                f"the login Keychain did not respond ({why}); saved logins fall back "
                "to files in the data directory",
                fix="security unlock-keychain",
            ))
    else:
        creds = _abbr(dd / "credentials", f.home)
        usable = True
        if exists:
            usable, _ = _try(p.access, dd, os.R_OK | os.W_OK | os.X_OK, default=True)
        if usable:
            checks.append(Check(
                "data.store", "data", PASS, f"saved logins are kept in files in {creds}",
            ))
        else:
            checks.append(Check(
                "data.store", "data", FAIL,
                f"saved logins are kept in {creds}, but you cannot write to {shown}",
                fix=f"chmod u+rwx {shown}",
            ))
    return checks


def _menubar_running_check(p: Probes) -> Check:
    result, err = _try(p.launchctl_print)
    if err is not None or not isinstance(result, tuple):
        return Check(
            "menubar.running", "menubar", WARN,
            f"could not ask launchd whether it runs ({_reason(err)})",
            fix="launchctl print gui/$(id -u)/com.aiquotaswap.menubar",
        )
    code, text = result
    if code != 0:
        return Check(
            "menubar.running", "menubar", WARN, "set up but not loaded by launchd",
            fix="qswap menubar",
        )
    job = parse_launchctl_print(text)
    if job["pid"] is not None:
        return Check("menubar.running", "menubar", PASS, f"running (pid {job['pid']})")
    if job["last_signal"]:
        return Check(
            "menubar.running", "menubar", FAIL,
            f"not running; it was stopped by a signal ({job['last_signal']})",
            fix="qswap menubar", hint=MENUBAR_LOG_HINT,
        )
    if job["last_exit"] is not None and job["last_exit"] != 0:
        return Check(
            "menubar.running", "menubar", FAIL,
            f"not running; it last exited with code {job['last_exit']}",
            fix="qswap menubar", hint=MENUBAR_LOG_HINT,
        )
    if job["last_exit"] == 0:
        # A deliberate Quit: a clean exit, which launchd leaves stopped.
        return Check("menubar.running", "menubar", PASS, "not running (it was quit)",
                     hint="qswap menubar starts it again")
    state = job["state"] or "unknown"
    return Check(
        "menubar.running", "menubar", WARN, f"loaded but not running (state {state})",
        fix="qswap menubar",
    )


def _plist_program(p: Probes, plist: Path | None) -> str | None:
    if plist is None:
        return None
    raw, _ = _try(p.read_bytes, plist)
    if not raw:
        return None
    try:
        data = plistlib.loads(raw)
    except Exception:
        return None
    args = data.get("ProgramArguments") if isinstance(data, dict) else None
    if isinstance(args, list) and args and isinstance(args[0], str):
        return args[0]
    return None


def _menubar_checks(p: Probes, f: _Facts) -> list[Check]:
    if f.platform != Platform.MACOS:
        if f.data_dir is None:
            return []
        settings = f.data_dir / "menubar_settings.json"
        present, _ = _try(p.exists, settings, default=False)
        if not present:
            return []
        return [Check(
            "menubar.platform", "menubar", WARN, "the menu bar is macOS-only; it cannot run here",
            fix="qswap auto switches accounts on this platform",
            hint=f"remove {_abbr(settings, f.home)} to clear this warning",
        )]

    checks: list[Check] = []
    if not f.menubar_configured:
        checks.append(Check("menubar.service", "menubar", PASS, "not set up",
                            hint="qswap menubar starts it, now and at every login"))
    else:
        checks.append(Check(
            "menubar.service", "menubar", PASS,
            "set up · starts at login" if f.menubar_login else "set up · Start at login is off",
        ))
        checks.append(_menubar_running_check(p))

    installed, _ = _try(p.install_paths, default=[])
    offenders: list[tuple[str, str]] = []
    if f.home is not None:
        for path in installed or []:
            folder = under_protected_folder(str(path), f.home)
            if folder:
                offenders.append((str(path), folder))
        program = _plist_program(p, f.menubar_plist)
        program_folder = under_protected_folder(program, f.home) if program else None
    else:
        program, program_folder = None, None

    from ai_quota_swap.update_check import menubar_install_spec

    # With the extra: the copy this installs is the one the menu bar runs, and
    # without rumps `qswap menubar` stops before it starts anything.
    reinstall = f"uv tool install --force {menubar_install_spec()}"
    if offenders:
        path, folder = offenders[0]
        fix = reinstall + (", then qswap menubar" if f.menubar_configured else "")
        checks.append(Check(
            "menubar.location", "menubar", FAIL,
            f"qswap runs from {_abbr(path, f.home)}; macOS keeps background "
            f"services out of ~/{folder}",
            fix=fix,
        ))
    elif program and program_folder:
        checks.append(Check(
            "menubar.location", "menubar", FAIL,
            f"the menu bar service runs {_abbr(program, f.home)}; macOS keeps "
            f"background services out of ~/{program_folder}",
            fix="qswap menubar",
        ))
    elif installed:
        checks.append(Check(
            "menubar.location", "menubar", PASS,
            "installed outside ~/Documents, ~/Desktop and ~/Downloads",
        ))
    return checks


def _auto_checks(p: Probes, f: _Facts) -> list[Check]:
    from ai_quota_swap import autoswitch
    from ai_quota_swap.display import ago, provider_name

    if f.data_dir is None:
        return []
    now, _ = _try(p.now, default=None)
    if not isinstance(now, (int, float)):
        now = time.time()
    providers = [("claude", f.data_dir)]
    codex_dir = paths.get_codex_data_dir(f.data_dir)
    codex_beat, _ = _try(p.heartbeat, codex_dir)
    if f.codex_present or codex_beat:
        providers.append(("codex", codex_dir))
    checks: list[Check] = []
    for provider, directory in providers:
        name = provider_name(provider)
        beat, err = (codex_beat, None) if provider == "codex" else _try(p.heartbeat, directory)
        if provider == "codex":
            start = "qswap codex auto"
        elif f.platform == Platform.MACOS:
            start = "qswap auto or qswap menubar --auto-switch"
        else:
            start = "qswap auto"
        hint = f"start one with {start}"
        beat = beat if isinstance(beat, dict) else None
        last = beat.get("lastTickAt") if beat else None
        ticked = ago(now - last) if isinstance(last, (int, float)) else ""
        running = bool(beat) and autoswitch.engine_running(beat, now)
        if running:
            pid = beat.get("pid")
            facts = [f"{name}: running" + (f" (pid {pid})" if isinstance(pid, int) else "")]
            if ticked:
                facts.append(f"last ticked {ticked}")
            if beat.get("dryRun"):
                facts.append("dry run, switches nothing")
            checks.append(Check(f"auto.{provider}", "auto", PASS, " · ".join(facts)))
        else:
            summary = f"{name}: not running"
            if ticked:
                summary += f" · last ticked {ticked}"
            checks.append(Check(f"auto.{provider}", "auto", PASS, summary, hint=hint))
    return checks


def _install_checks(p: Probes, f: _Facts) -> list[Check]:
    from ai_quota_swap.update_check import describe_identity

    info, err = _try(p.install_info, default=None)
    info = info if isinstance(info, dict) else {}
    python = info.get("python") or _platform.python_version()
    checks = [Check("install.python", "install", PASS, f"Python {python}")]
    build = describe_identity(info.get("identity")) if info.get("identity") else "unknown"
    method = info.get("method")
    if err is not None:
        how = f"install method unknown ({_reason(err)})"
    elif method == "uv":
        how = "installed with uv tool"
    elif method == "pipx":
        how = "installed with pipx"
    elif info.get("editable"):
        how = "installed from a source checkout (editable)"
    else:
        how = "installed with pip or from source"
    checks.append(Check("install.method", "install", PASS, f"ai-quota-swap {build} · {how}"))
    return checks


def _network_checks(p: Probes, f: _Facts) -> list[Check]:
    checks: list[Check] = []
    fix = "check the network connection and any proxy (HTTPS_PROXY)"
    for check_id, url, needed in (
        ("network.anthropic", ANTHROPIC_URL, True),
        ("network.chatgpt", CHATGPT_URL, f.codex_present),
    ):
        host = url.split("//", 1)[1].rstrip("/")
        answer, err = _try(p.reach, url)
        if err is None:
            checks.append(Check(check_id, "network", PASS, f"{host} answers ({answer})"))
        else:
            checks.append(Check(
                check_id, "network", FAIL if needed else WARN,
                f"cannot reach {host} ({_reason(err)})", fix=fix,
            ))
    return checks


def _gather_facts(p: Probes) -> _Facts:
    from ai_quota_swap import launch_agent

    platform, _ = _try(p.platform, default=Platform.UNKNOWN)
    if not isinstance(platform, Platform):
        platform = Platform.UNKNOWN
    home, _ = _try(p.home)
    data_dir, _ = _try(p.data_dir)
    codex_home, _ = _try(p.codex_home)
    claude_bin, _ = _try(p.which, "claude")
    codex_bin, _ = _try(p.which, "codex")
    facts = _Facts(
        platform=platform,
        home=Path(home) if home is not None else None,
        data_dir=Path(data_dir) if data_dir is not None else None,
        codex_home=Path(codex_home) if codex_home is not None else None,
        claude_bin=claude_bin or None,
        codex_bin=codex_bin or None,
    )
    if facts.data_dir is not None:
        registry, err = _read_json(p, paths.get_codex_data_dir(facts.data_dir) / "sequence.json")
        facts.codex_registry, facts.codex_registry_error = registry, err
    codex_files = False
    if facts.codex_home is not None:
        for name in ("auth.json", "config.toml"):
            found, _ = _try(p.exists, facts.codex_home / name, default=False)
            codex_files = codex_files or bool(found)
    facts.codex_present = bool(
        facts.codex_bin or codex_files or _accounts(facts.codex_registry)
        or facts.codex_registry_error is not None
    )
    if platform == Platform.MACOS and facts.home is not None:
        login = launch_agent.plist_path(home=facts.home)
        login_found, _ = _try(p.exists, login, default=False)
        session_found = False
        session = None
        if facts.data_dir is not None:
            session = launch_agent.session_plist_path(backup_dir=facts.data_dir)
            session_found, _ = _try(p.exists, session, default=False)
        facts.menubar_configured = bool(login_found or session_found)
        facts.menubar_login = bool(login_found)
        facts.menubar_plist = login if login_found else (session if session_found else None)
    return facts


_SECTIONS: tuple[tuple[str, Callable[[Probes, _Facts], list[Check]]], ...] = (
    ("claude", _claude_checks),
    ("claude", _claude_override_checks),
    ("codex", _codex_checks),
    ("data", _data_checks),
    ("menubar", _menubar_checks),
    ("auto", _auto_checks),
    ("install", _install_checks),
)


def run_checks(probes: Probes | None = None, *, online: bool = False) -> list[Check]:
    """Every check, in output order. Never raises: a section that fails
    unexpectedly becomes one ``warn`` line saying so."""
    p = probes if probes is not None else default_probes()
    facts = _gather_facts(p)
    sections = list(_SECTIONS)
    if online:
        sections.append(("network", _network_checks))
    checks: list[Check] = []
    for group, section in sections:
        try:
            checks.extend(section(p, facts))
        except Exception as e:  # a bug here must not end the report
            checks.append(Check(
                f"{group}.error", group, WARN,
                f"this check could not run ({type(e).__name__}: {_reason(e)})",
                fix="qswap doctor --json shows the rest; please report this",
            ))
    return checks


def counts(checks: Iterable[Check]) -> dict[str, int]:
    """``{"pass": n, "warn": n, "fail": n, "skip": n}``."""
    out = dict.fromkeys(STATUSES, 0)
    for check in checks:
        out[check.status] = out.get(check.status, 0) + 1
    return out


def exit_code(checks: Iterable[Check]) -> int:
    """1 when any check failed, else 0."""
    return 1 if any(c.status == FAIL for c in checks) else 0


# -- output -------------------------------------------------------------------------


def to_json(checks: list[Check], *, online: bool) -> dict:
    """The ``--json`` payload."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "checks": [c.to_json() for c in checks],
        "counts": counts(checks),
        "online": online,
    }


def summary_line(tally: Mapping[str, int]) -> str:
    """``"2 warnings, no failures"``."""

    def phrase(n: int, word: str) -> str:
        if n == 0:
            return f"no {word}s"
        return f"{n} {word}{'' if n == 1 else 's'}"

    return f"{phrase(tally.get(WARN, 0), 'warning')}, {phrase(tally.get(FAIL, 0), 'failure')}"


def _mark_style(status: str) -> Callable[[str], str]:
    from ai_quota_swap import printer

    return {
        PASS: printer.muted,
        WARN: printer.yellowed,
        FAIL: printer.reddened,
        SKIP: printer.dimmed,
    }.get(status, printer.muted)


def _check_lines(check: Check, width: int) -> list[str]:
    from ai_quota_swap.display import fit, wrap
    from ai_quota_swap.printer import dimmed, muted

    body = " " * 8
    lines = [fit(line, width) for line in wrap(check.summary, width, body)]
    mark = MARKS.get(check.status, check.status)
    first = f"  {_mark_style(check.status)(mark.ljust(4))}  "
    rest_style = dimmed if check.status == SKIP else (lambda s: s)
    out = [first + rest_style(lines[0][8:])] + [rest_style(line) for line in lines[1:]]
    if check.path:
        out.append(body + rest_style(check.path))  # whole, even past the width
    if check.fix and check.status in (WARN, FAIL):
        fix_lines = [fit(line, width) for line in wrap(f"fix: {check.fix}", width, body, " " * 13)]
        out.append(body + muted("fix:") + fix_lines[0][8 + 4:])
        out.extend(fix_lines[1:])
    if check.hint:
        out.extend(dimmed(fit(line, width)) for line in wrap(check.hint, width, body))
    return out


def render_text(checks: list[Check], *, online: bool, width: int = 80) -> list[str]:
    """The text report, one string per line, each at most ``width`` columns."""
    from ai_quota_swap.display import fit, wrap
    from ai_quota_swap.printer import bolded, dimmed, muted, reddened, yellowed

    width = max(40, min(80, width))
    lines: list[str] = []
    for group, title in GROUPS:
        members = [c for c in checks if c.group == group]
        if not members:
            continue
        if lines:
            lines.append("")
        lines.append(bolded(title))
        for check in members:
            lines.extend(_check_lines(check, width))
    if not online:
        lines.append("")
        note = "Network checks skipped; run qswap doctor --online to include them."
        lines.extend(dimmed(fit(line, width)) for line in wrap(note, width))
    tally = counts(checks)
    text = summary_line(tally)
    style = reddened if tally[FAIL] else yellowed if tally[WARN] else muted
    lines.append("")
    lines.append(style(text))
    return lines


# -- the command --------------------------------------------------------------------


def _parser():
    from ai_quota_swap.argparsing import ArgumentParser

    parser = ArgumentParser(
        prog="qswap doctor",
        description=(
            "Check that qswap, Claude Code and Codex are set up and working, and\n"
            "say how to fix anything that is not. Reads only; changes nothing."
        ),
        epilog=(
            "examples:\n"
            "  qswap doctor\n"
            "  qswap doctor --online      # also check the network\n"
            "  qswap doctor --json"
        ),
    )
    parser.add_argument("--json", action="store_true", help="print the checks as JSON")
    parser.add_argument(
        "--online", action="store_true",
        help="also check that api.anthropic.com and chatgpt.com can be reached",
    )
    return parser


def doctor_command(argv: list[str], *, probes: Probes | None = None) -> None:
    """``qswap doctor [--json] [--online]``. Exits 1 when a check fails,
    64 on a usage error; returns normally otherwise."""
    from ai_quota_swap.argparsing import help_width

    args = _parser().parse_args(argv)
    try:
        checks = run_checks(probes, online=args.online)
    except KeyboardInterrupt:
        print("Cancelled; nothing was changed.", file=sys.stderr)
        sys.exit(130)
    if args.json:
        print(json.dumps(to_json(checks, online=args.online), indent=2))
    else:
        for line in render_text(checks, online=args.online, width=help_width()):
            print(line)
    code = exit_code(checks)
    if code:
        sys.exit(code)
