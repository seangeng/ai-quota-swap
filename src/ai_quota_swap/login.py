"""``qswap login``: sign a saved Claude Code account in again, in the browser,
without touching the login Claude Code is using.

A subscription login carries ``refreshTokenExpiresAt``, a deadline set at
sign-in that only a new sign-in moves (``oauth.login_time_left``). Renewing it
used to take ``/login`` inside Claude Code as that account, which replaces the
live login, and then ``qswap add``. Here ``claude auth login --claudeai
--email EMAIL`` runs with ``CLAUDE_CONFIG_DIR`` pointed at a private profile,
so Claude Code keeps the new login apart from the live one: in a Keychain item
named after the profile on macOS (``session.keychain_service_name``), in the
profile's ``.credentials.json`` elsewhere. The profile's identity must be the
account's; the login is then saved with
:meth:`ClaudeAccountSwitcher.save_renewed_login`, which also makes it live
when the account is the active one.

The private profiles live in ``<data directory>/sign-in/``, which nothing else
reads: they are not ``qswap run`` profiles (``sessions/``) and are never
reused. One sign-in runs at a time, under ``sign-in/.lock``, so whatever else
is in that directory when the lock is taken was left by a run that crashed,
and is removed then. Every run deletes its profile's Keychain item and then
the directory, whatever happened; a directory whose item could not be deleted
stays, emptied, since its path is the only way to find the item again.
``claude auth logout`` is never run: it would revoke the login just made.

Off a terminal (the menu bar) the child's output is captured, its stdin is
empty and the wait is bounded by the timeout; on one it shares the terminal,
so Claude Code's own prompts show, and Ctrl-C ends both.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ai_quota_swap import clis, display, macos_keychain, oauth, prompts, spawn
from ai_quota_swap.exceptions import (
    AccountNotFoundError,
    CredentialReadError,
    LockError,
    LoginError,
    ValidationError,
)
from ai_quota_swap.locking import FileLock
from ai_quota_swap.models import Platform

_logger = logging.getLogger("ai-quota-swap")

#: Under the data directory: the private profiles of sign-ins in progress.
SIGN_IN_DIRNAME = "sign-in"
_LOCK_NAME = ".lock"
_PROFILE_PREFIX = "profile-"
#: How long a sign-in may take before it is stopped (``--timeout``).
DEFAULT_TIMEOUT_S = 300.0
#: How long to wait for another sign-in's lock before saying one is running.
_LOCK_WAIT_S = 0.5
#: The longest line of ``claude``'s own output a failure message quotes.
_DETAIL_LIMIT = 160


@dataclass(frozen=True)
class LoginRun:
    """How ``claude auth login`` ended: its exit code, or None when it never
    ran or was stopped (``timed_out``); ``detail`` says why it never ran, or
    is the last line it printed when its output was captured."""

    returncode: int | None
    detail: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class Target:
    """A subscription login ``qswap login`` can renew: its account, its
    deadline (None: the login does not record one) and whether it already
    needs re-login (``why``)."""

    number: str
    email: str
    org_uuid: str
    label: str
    expires_at: float | None = None
    needs_relogin: bool = False
    why: str = ""
    #: The live login is this account's, so a renewal replaces it too.
    active: bool = False

    def due(self, now: float) -> bool:
        return is_due(self.expires_at, self.needs_relogin, now)

    def note(self, now: float) -> str:
        return due_note(self.expires_at, self.needs_relogin, now)


def is_due(expires_at: float | None, needs_relogin: bool, now: float) -> bool:
    """Whether a login wants a sign-in now: it needs re-login, or runs out
    within ``oauth.LOGIN_DUE_WITHIN_S``. The menu's *Sign in again* asks the
    same question."""
    if needs_relogin:
        return True
    return expires_at is not None and expires_at - now <= oauth.LOGIN_DUE_WITHIN_S


def due_note(expires_at: float | None, needs_relogin: bool, now: float) -> str:
    """``due in 13h``, ``needs re-login`` once the deadline is past, or ``""``
    when the login does not record one."""
    if needs_relogin or (expires_at is not None and expires_at <= now):
        return display.NEEDS_RELOGIN
    if expires_at is None:
        return ""
    return f"due in {oauth.format_countdown(expires_at - now)}"


@dataclass(frozen=True)
class Renewal:
    """A login renewed: the account, its new deadline, whether the live
    login was replaced too, and the API key active over it (``key``)."""

    number: str
    label: str
    expires_at: float | None
    live: bool = False
    key: str | None = None
    #: The account is the live login's, and that was left as it was: why
    #: (``empty``, ``no-login``, ``api-key``, ``foreign``).
    live_left: str | None = None
    #: Sentences for the caller to show (an ownership check that could not run).
    notes: tuple[str, ...] = ()


class SignInInterrupted(KeyboardInterrupt):
    """Ctrl-C during a sign-in, with what had been written by then: the
    live login (``live``) and the saved one (``saved``). Before the save
    both are False and nothing was written."""

    def __init__(self, *, live: bool = False, saved: bool = False):
        super().__init__()
        self.live = live
        self.saved = saved


# -- which logins -----------------------------------------------------------------


def _codex_account_named(switcher, identifier: str) -> bool:
    """Whether ``identifier`` (an alias or an email, never a number) names a
    Codex account, from Codex's registry file alone."""
    from ai_quota_swap.paths import get_codex_data_dir

    if not identifier or identifier.isdigit():
        return False
    try:
        data = json.loads((get_codex_data_dir(switcher.backup_dir) / "sequence.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        return False
    key = identifier.lower()
    return any(
        isinstance(record, dict)
        and ((record.get("alias") or "").lower() == key or record.get("email") == identifier)
        for record in accounts.values()
    )


def _is_setup_token(creds: str) -> bool:
    return oauth.is_setup_token(oauth.extract_oauth_data(creds or ""))


def resolve(switcher, identifier: str) -> Target:
    """The account ``identifier`` names, when it is a subscription login.

    Refuses, in one sentence that says what to do instead, an API-key
    account, a setup-token account and a Codex account."""
    try:
        number, email, org_uuid = switcher.resolve_account(identifier)
    except AccountNotFoundError:
        if _codex_account_named(switcher, identifier):
            raise ValidationError(
                f"{identifier} is a Codex account (experimental), and qswap login "
                "renews Claude Code logins; sign it in again with: qswap codex login"
            ) from None
        raise
    name = switcher.label(number, long=False)
    if switcher.account_kind_for(number) == "api_key":
        raise ValidationError(
            f"{name} is an API-key account, which has no sign-in to renew; to "
            f"replace its key: qswap add-token KEY --slot {number}"
        )
    target = _Facts.read(switcher).target(number)
    if target is None:
        raise ValidationError(
            f"{name} holds a setup-token, which a sign-in does not renew; make a "
            f"new one with claude setup-token, then: qswap add-token TOKEN --slot {number}"
        )
    return target


@dataclass(frozen=True)
class _Facts:
    """What every target is read against, read once: the registry, the
    accounts that need re-login, and the live login."""

    switcher: object
    accounts: dict
    order: list
    relogin: dict
    live_num: str | None
    live_creds: str | None

    @classmethod
    def read(cls, switcher) -> _Facts:
        data = switcher._get_sequence_data() or {}
        accounts = data.get("accounts") or {}
        order = [str(n) for n in data.get("sequence") or []]
        order += [n for n in accounts if n not in order]
        try:
            relogin = switcher.relogin_reasons()
        except Exception as e:  # a display question; never fail the command on it
            _logger.debug(f"login: could not read which accounts need re-login: {e!r}")
            relogin = {}
        try:
            live_num = switcher.login_account_number()
            live_creds = switcher._read_credentials() if live_num else None
        except Exception:
            live_num, live_creds = None, None
        return cls(switcher, accounts, order, relogin, live_num, live_creds)

    def target(self, num: str) -> Target | None:
        """Account ``num`` as a :class:`Target`; None for an API key or a
        setup-token. Its deadline is its saved login's; for the live login's
        account, the later of that and the live credential's (a ``/login``
        made in Claude Code moves the live one first). No saved login at all
        needs re-login, as does an account ``relogin_reasons`` names."""
        record = self.accounts.get(num)
        if not isinstance(record, dict) or record.get("kind") == "api_key":
            return None
        email = record.get("email", "")
        creds, unreadable = self.switcher._read_account_credentials_ex(num, email)
        if _is_setup_token(creds):
            return None
        expires = oauth.login_expires_at_epoch(creds) if creds else None
        live = self.live_creds if num == self.live_num else None
        if live and not _is_setup_token(live):
            live_expires = oauth.login_expires_at_epoch(live)
            if live_expires is not None and (expires is None or live_expires > expires):
                expires = live_expires
        why = self.relogin.get(num, "")
        if not creds and not unreadable and not why:
            why = "no saved login"
        return Target(
            num, email, record.get("organizationUuid", "") or "",
            self.switcher.label(num, long=False), expires_at=expires,
            needs_relogin=bool(why), why=why, active=num == self.live_num,
        )


def subscription_logins(switcher) -> list[Target]:
    """Every Claude Code account a sign-in renews, in account order: not API
    keys, not setup-tokens (:meth:`_Facts.target`)."""
    facts = _Facts.read(switcher)
    return [t for t in (facts.target(num) for num in facts.order) if t is not None]


def due_first(targets: list[Target], now: float) -> list[Target]:
    """Due ones first (those that need re-login, then the soonest), the
    rest in the order given."""
    def key(item: tuple[int, Target]):
        i, t = item
        if t.needs_relogin:
            return (0, 0.0, i)
        if t.due(now):
            return (1, t.expires_at or 0.0, i)
        return (2, 0.0, i)

    return [t for _, t in sorted(enumerate(targets), key=key)]


def nothing_due_text(targets: list[Target], now: float) -> str:
    """What a bare ``qswap login`` says when no login is due."""
    if not targets:
        return (
            "No subscription logins are managed, so there is nothing to sign in "
            "again. API keys and setup-tokens have no sign-in to renew."
        )
    dated = sorted((t for t in targets if t.expires_at is not None), key=lambda t: t.expires_at)
    if not dated:
        return (
            "No login is due within 7 days. The saved logins do not record when they "
            f"run out; to renew one anyway: qswap login {targets[0].number}"
        )
    t = dated[0]
    when = oauth.login_expiry_clock(t.expires_at, now)
    return (
        f"No login is due within 7 days. The next is {t.label}, due {when} · in "
        f"{oauth.format_countdown(t.expires_at - now)}; to renew it now: qswap login {t.number}"
    )


def renewed_text(renewal: Renewal, now: float | None = None) -> str:
    """``Renewed the login of #3 work; the next sign-in is due Dec 23 14:02 · in 30d``."""
    text = f"Renewed the login of {renewal.label}"
    if renewal.expires_at is None:
        return text
    now = time.time() if now is None else now
    when = oauth.login_expiry_clock(renewal.expires_at, now)
    left = oauth.format_countdown(renewal.expires_at - now)
    return f"{text}; the next sign-in is due {when} · in {left}"


def key_note(switcher, renewal: Renewal) -> str | None:
    """While an API key is active over the renewed login: sessions stay on
    the key, and the switch that puts them on this login (``add``'s words)."""
    if renewal.key is None or str(renewal.key) == str(renewal.number):
        return None
    return " ".join(display.key_still_active(switcher.label(renewal.key, long=False), renewal.number))


def browser_note(label: str, email: str, *, active: bool = False) -> str:
    """What to know before the browser opens. ``active``: the account is the
    live login's, which the new login replaces too."""
    head = (
        f"The browser opens to sign in {label} ({email}). It must be signed in to "
        f"claude.ai as {email}, or signed out."
    )
    if active:
        return f"{head} It is the active account, so the new login becomes the live one too."
    return f"{head} The active login is not touched."


_LIVE_LEFT_WHY = {
    "empty": "Claude Code's login store holds no login",
    "no-login": "Claude Code's login store holds only MCP server sign-ins",
    "api-key": "Claude Code's login store holds an API key",
    "foreign": "the live login belongs to another account",
}


def live_left_note(renewal: Renewal) -> str | None:
    """When the account is the active one but its live login was left as it
    was: that, why, and the switch that installs the renewed login. A plain
    ``qswap switch N`` to the active account changes nothing, so ``--force``."""
    if not renewal.live_left:
        return None
    why = _LIVE_LEFT_WHY.get(renewal.live_left, renewal.live_left)
    return (
        f"{renewal.label} is the active account, but its live login was left as it "
        f"was ({why}); to install the renewed one: qswap switch {renewal.number} --force"
    )


def uuid_mismatch_text(label: str, number: str, email: str) -> str:
    """The same email and organization as the account saved in the slot, but
    another account id: that account was deleted and made again."""
    return (
        f"The browser signed in as {email}, but as another account than the one "
        f"saved as {label} (its account id differs), so nothing was saved. If that "
        f"account was deleted and made again, log in to it in Claude Code (/login), "
        f"then run: qswap add --slot {number}"
    )


# -- the sign-in ------------------------------------------------------------------


def sign_in_root(switcher) -> Path:
    return Path(switcher.backup_dir) / SIGN_IN_DIRNAME


#: Dropped from the sign-in's environment alone, on top of
#: ``session.AUTH_OVERRIDE_ENV_VARS``: with ``CLAUDE_CODE_OAUTH_REFRESH_TOKEN``
#: set, ``claude auth login`` (2.1.330) skips the browser and exchanges that
#: token, spending it; the others steer which login it makes.
#: ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` would put the login in another
#: profile's store (with it empty, the default one: the live login).
SIGN_IN_DROPPED_ENV_VARS = (
    "CLAUDE_SECURESTORAGE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_SCOPES",
    "CLAUDE_CODE_OAUTH_CLIENT_ID",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
)


def login_env(profile: Path) -> dict[str, str]:
    """The child's environment: ``CLAUDE_CONFIG_DIR`` is the private profile,
    and nothing sends the sign-in elsewhere or makes it without the browser:
    ``session.AUTH_OVERRIDE_ENV_VARS`` and :data:`SIGN_IN_DROPPED_ENV_VARS`
    are dropped."""
    from ai_quota_swap.session import _probe_env

    env = _probe_env(profile)
    for name in SIGN_IN_DROPPED_ENV_VARS:
        env.pop(name, None)
    return env


#: Characters ``cmd.exe`` reads in a ``claude.cmd`` shim's arguments.
_CMD_SPECIALS = '&|<>^%!"()'


def cmd_shim_refusal(email: str, exe: str | None, platform: str = sys.platform) -> str | None:
    """Why ``email`` cannot be passed to ``claude`` here, or None. On Windows
    a ``claude.cmd`` (or ``.bat``) shim hands its arguments to ``cmd.exe``,
    which re-reads characters such as ``&`` and ``%`` in them."""
    if platform != "win32" or not exe or not exe.lower().endswith((".cmd", ".bat")):
        return None
    if not any(c in email for c in _CMD_SPECIALS):
        return None
    return (
        f"{email} has a character that Windows reads differently in claude.cmd "
        f"({' '.join(_CMD_SPECIALS)}), so qswap login cannot pass it on; log in to it "
        "in Claude Code (/login), then run: qswap add"
    )


def _save_terminal():
    """The terminal's settings, to put back after the child: a child killed
    at the timeout cannot restore the raw mode it set."""
    try:
        import termios

        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return None
        return fd, termios.tcgetattr(fd)
    except Exception:
        return None


def _restore_terminal(saved) -> None:
    if saved is None:
        return
    try:
        import termios

        termios.tcsetattr(saved[0], termios.TCSADRAIN, saved[1])
    except Exception:
        pass


def _last_line(*texts: str | None) -> str:
    for text in texts:
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        if lines:
            return lines[-1][:_DETAIL_LIMIT]
    return ""


def run_claude_login(
    profile: Path, email: str, *, sso: bool = False, interactive: bool = True,
    timeout: float | None = DEFAULT_TIMEOUT_S,
    on_process: Callable[[subprocess.Popen], None] | None = None,
) -> LoginRun:
    """``claude auth login --claudeai --email EMAIL [--sso]`` into ``profile``.

    On a terminal the child shares it: Claude Code's prompts show and Ctrl-C
    reaches it (same process group). Claude Code handles SIGINT itself and
    exits 130, so the parent may see no ``KeyboardInterrupt``; :func:`renew`
    reads that exit as a cancel. The terminal's settings are saved and put
    back around the child. Off a terminal, stdin is empty and the output is
    captured; the failure detail is the last line of stderr alone (stdout
    carries the prompt and the authorize URL). Either way the child is
    killed at ``timeout``. ``on_process`` receives the running process, for
    a caller that cancels it (the menu bar).
    """
    exe = clis.find_cli("claude")
    if exe is None:
        return LoginRun(None, "claude is not on PATH")
    argv = [exe, "auth", "login", "--claudeai", "--email", email]
    if sso:
        argv.append("--sso")
    kwargs: dict = {"env": login_env(profile)}
    if not interactive:
        kwargs.update(
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    terminal = _save_terminal() if interactive else None
    try:
        try:
            proc = spawn.popen(argv, **kwargs)
        except OSError as e:
            return LoginRun(None, str(e))
        if on_process is not None:
            on_process(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return LoginRun(None, "timed out", timed_out=True)
        except BaseException:
            proc.kill()
            proc.wait()
            raise
    finally:
        _restore_terminal(terminal)
    if out or err:
        _logger.debug("claude auth login said: %s", _last_line(err, out))
    return LoginRun(proc.returncode, "" if interactive else _last_line(err))


def _delete_keychain_item(profile: Path) -> bool:
    """Delete the Keychain item Claude Code keeps for ``profile``; True when
    it is gone (or there is no Keychain). The name hashes the exact string
    ``CLAUDE_CONFIG_DIR`` was given, which is ``str(profile)``."""
    if Platform.detect() != Platform.MACOS:
        return True
    from ai_quota_swap.session import keychain_service_name

    try:
        macos_keychain.delete_password(
            keychain_service_name(str(profile)), macos_keychain.keychain_account_name()
        )
        return True
    except macos_keychain.KEYCHAIN_ERRORS as e:
        _logger.warning(f"Could not delete the Keychain item of sign-in profile {profile}: {e}")
        return False


def remove_profile(profile: Path) -> bool:
    """Delete a private profile and its Keychain item; True when both are
    gone. Never raises. Without the item gone the directory stays, emptied
    of everything a login could be in, for the next ``qswap login`` to retry:
    its path is the only way to find the item."""
    if profile.is_symlink():
        with contextlib.suppress(OSError):
            profile.unlink()
        return True
    item_gone = _delete_keychain_item(profile)
    if item_gone:
        try:
            shutil.rmtree(profile)
            return True
        except FileNotFoundError:
            return True
        except OSError as e:
            _logger.warning(f"Could not delete sign-in profile {profile}: {e}")
    try:
        for child in profile.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    child.unlink()
    except OSError:
        pass
    return False


def sweep(root: Path) -> None:
    """Remove what earlier sign-ins left in ``root``; the caller holds the
    lock, so no sign-in is running. Never raises."""
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name == _LOCK_NAME:
            continue
        if entry.is_dir() and not entry.is_symlink():
            remove_profile(root / entry.name)
        else:
            with contextlib.suppress(OSError):
                entry.unlink()


def _make_root(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            os.chmod(root, 0o700)


def _profile_oauth_account(profile: Path) -> dict:
    try:
        config = json.loads((profile / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    account = config.get("oauthAccount") if isinstance(config, dict) else None
    return account if isinstance(account, dict) else {}


def _failure(run: LoginRun, target: Target, timeout: float | None) -> LoginError:
    again = f"qswap login {target.number}"
    if run.timed_out:
        took = oauth.format_countdown(timeout or 0)
        return LoginError(
            f"The browser sign-in for {target.label} did not finish within {took}, so "
            f"nothing was saved. Try again with more time: {again} --timeout 600"
        )
    if run.returncode is None:
        return LoginError(
            f"Could not run claude auth login ({run.detail}), so nothing was saved. "
            f"Install Claude Code or put it on PATH, then run: {again}"
        )
    said = f": {run.detail.rstrip('.')}" if run.detail else ""
    return LoginError(
        f"claude auth login failed (exit code {run.returncode}){said}. Nothing was "
        f"saved for {target.label}; try again: {again}"
    )


def _same_account(email: str, org_uuid: str, target: Target) -> bool:
    """The codebase's account identity: email (case aside) and organization."""
    return email.lower() == target.email.lower() and org_uuid == target.org_uuid


def _mismatch(switcher, target: Target, email: str, org_uuid: str, oauth_account: dict) -> LoginError:
    data = switcher._get_sequence_data() or {}
    other = next((
        num for num, record in (data.get("accounts") or {}).items()
        if isinstance(record, dict)
        and (record.get("email") or "").lower() == email.lower()
        and (record.get("organizationUuid") or "") == org_uuid
    ), None)
    same_email = email.lower() == target.email.lower()
    if other is not None:
        who = switcher.label(other, long=False)
    elif same_email:
        org = oauth_account.get("organizationName") or ("another organization" if org_uuid else "personal")
        who = f"{email} ({org})"
    else:
        who = email
    record = (data.get("accounts") or {}).get(target.number) or {}
    expected = target.email
    if same_email:
        org = record.get("organizationName") or ("its organization" if target.org_uuid else "the personal account")
        expected = f"{target.email} in {org}"
    text = (
        f"The browser signed in as {who}, not {target.label} ({target.email}), so "
        f"nothing was saved. Sign out of claude.ai in the browser, or switch it to "
        f"{expected}, and run it again: qswap login {target.number}"
    )
    if other is not None and other != target.number:
        text += f". To renew {who} instead: qswap login {other}"
    return LoginError(text)


def _still_the_target(switcher, target: Target) -> None:
    """Refuse when the slot no longer holds the account ``target`` was read
    as: a bare ``qswap login`` reads every target once, before the first
    of several browser waits."""
    record = ((switcher._get_sequence_data() or {}).get("accounts") or {}).get(target.number)
    if not isinstance(record, dict) or not _same_account(
        record.get("email") or "", record.get("organizationUuid") or "", target
    ):
        raise LoginError(
            f"{target.label} ({target.email}) changed before its sign-in started, so "
            "nothing was signed in. See where the accounts are now with: qswap list"
        )


#: How long a save waits out a busy lock (qswap's or Claude Code's) before
#: giving up; a completed sign-in is worth the wait. ``save_renewed_login``
#: raises before it writes anything.
_SAVE_RETRY_S = 45.0
_SAVE_RETRY_PAUSE_S = 1.0


def _save(switcher, target: Target, creds: str, oauth_account: dict, save_lock, progress: dict) -> dict:
    deadline = time.monotonic() + _SAVE_RETRY_S
    while True:
        try:
            with save_lock if save_lock is not None else contextlib.nullcontext():
                return switcher.save_renewed_login(
                    target.number, creds, oauth_account, progress=progress
                )
        except LockError as e:
            if time.monotonic() >= deadline:
                raise LoginError(
                    f"The account files stayed busy for {oauth.format_countdown(_SAVE_RETRY_S)} "
                    f"(another qswap or Claude Code was writing them), so the new login of "
                    f"{target.label} was not saved. Try again: qswap login {target.number}"
                ) from e
            _logger.info("login: %s; retrying the save of #%s", e, target.number)
            time.sleep(_SAVE_RETRY_PAUSE_S)


#: Exit statuses of a ``claude`` that Ctrl-C ended: its own 130, or SIGINT.
_CANCELLED = (130, -2)


def renew(
    switcher,
    target: Target,
    *,
    sso: bool = False,
    timeout: float | None = DEFAULT_TIMEOUT_S,
    interactive: bool | None = None,
    runner: Callable[..., LoginRun] | None = None,
    save_lock=None,
    announce: Callable[[], None] | None = None,
    on_process: Callable[[subprocess.Popen], None] | None = None,
) -> Renewal:
    """Sign ``target`` in again in a private profile and save the login.

    ``interactive`` defaults to whether stdin is a terminal. ``runner(profile,
    email, sso=, interactive=, timeout=[, on_process=]) -> LoginRun`` stands
    in for :func:`run_claude_login` (tests never run ``claude``); it is
    called with the profile already made. ``save_lock`` (a context manager)
    is held around the save alone, never during the wait for the browser;
    ``announce`` runs once the sign-in lock is held, before ``claude``
    starts; ``on_process`` is handed the child (the menu bar's cancel).

    The slot must still be ``target``'s account when the sign-in starts, the
    sign-in must land on that account, and :meth:`ClaudeAccountSwitcher.
    save_renewed_login` checks the slot against the sign-in again before it
    writes. A save that meets a busy lock is retried for
    :data:`_SAVE_RETRY_S`. Raises :class:`LoginError` with nothing saved;
    Ctrl-C (or, on a terminal, ``claude`` exiting the way Ctrl-C ends it)
    raises :class:`SignInInterrupted` once the profile is gone.
    """
    switcher._refuse_session_shell()
    if interactive is None:
        interactive = prompts.interactive()
    refusal = cmd_shim_refusal(target.email, clis.find_cli("claude"))
    if refusal:
        raise LoginError(refusal)
    _still_the_target(switcher, target)
    run = runner or run_claude_login
    root = sign_in_root(switcher)
    _make_root(root)
    lock = FileLock(root / _LOCK_NAME)
    if not lock.acquire(timeout=_LOCK_WAIT_S):
        raise LoginError(
            "Another sign-in is waiting for the browser (qswap login in another "
            "terminal, or the menu bar). Finish or cancel it, then try again."
        )
    progress: dict = {}
    try:
        sweep(root)
        made = Path(tempfile.mkdtemp(prefix=_PROFILE_PREFIX, dir=root))
        # The exact string exported, hashed into the Keychain item's name,
        # and rebuilt the same way by the sweep: root / name.
        profile = root / made.name
        try:
            if sys.platform != "win32":
                os.chmod(profile, 0o700)
            if announce is not None:
                announce()
            extra = {"on_process": on_process} if on_process is not None else {}
            outcome = run(profile, target.email, sso=sso, interactive=interactive,
                          timeout=timeout, **extra)
            if interactive and outcome.returncode in _CANCELLED:
                raise SignInInterrupted()
            if outcome.returncode != 0:
                raise _failure(outcome, target, timeout)
            from ai_quota_swap.session import read_config_dir_credentials, read_session_identity

            identity = read_session_identity(profile)
            if identity is None:
                raise LoginError(
                    f"claude auth login finished but left no signed-in account, so "
                    f"nothing was saved. Try again: qswap login {target.number}"
                )
            oauth_account = _profile_oauth_account(profile)
            email, org_uuid = identity
            if not _same_account(email, org_uuid, target):
                raise _mismatch(switcher, target, email, org_uuid, oauth_account)
            try:
                creds = read_config_dir_credentials(str(profile), strict_keychain=True)
            except CredentialReadError as e:
                raise LoginError(
                    "The new login could not be read from the Keychain (it is locked or "
                    f"busy), so nothing was saved. Once it answers, try again: qswap login {target.number}"
                ) from e
            if not creds:
                raise LoginError(
                    f"claude auth login finished but left no login to save. Nothing was "
                    f"saved; try again: qswap login {target.number}"
                )
            saved = _save(switcher, target, creds, oauth_account, save_lock, progress)
        except SignInInterrupted:
            raise
        except KeyboardInterrupt as e:
            raise SignInInterrupted(
                live=bool(progress.get("live")), saved=bool(progress.get("saved"))
            ) from e
        finally:
            remove_profile(profile)
    finally:
        lock.release()
    return Renewal(
        target.number, target.label, oauth.login_expires_at_epoch(creds),
        live=bool(saved.get("live")), key=saved.get("key"),
        live_left=saved.get("live_left"), notes=tuple(saved.get("notes") or ()),
    )


def renew_account(switcher, identifier: str, **kwargs) -> Renewal:
    """:func:`renew` for the account ``identifier`` names (:func:`resolve`)."""
    return renew(switcher, resolve(switcher, identifier), **kwargs)
