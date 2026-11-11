"""Live activation of API-key accounts (docs/design/api-key-sessions.md, part 2).

An API-key account is active exactly when qswap's ``apiKeyHelper`` line for it
is in Claude Code's user settings. Every Claude Code process that reads that
file moves to the key within a few seconds, running or new; the subscription
whose login is live stays logged in *underneath*, and sessions return to it
when the line is removed. Nothing else changes on the way: not the login
item, not the saved-key item, not ``~/.claude.json``.

This mixin gives :class:`~ai_quota_swap.switcher.ClaudeAccountSwitcher`:

- the active-account rule: :meth:`current_account_number` answers the key
  while its line is installed, :meth:`login_account_number` the live login
  (what the login switch itself works on), and
  :meth:`underneath_account_number` the subscription under an active key;
- the two steps of each switch direction (:meth:`_enter_key`,
  :meth:`_leave_key`), each with the run-profile step it needs: while the
  line is installed, every ``qswap run`` profile that shares the user
  settings gets a copy of them without the line (step one of entering), and
  the sharing comes back once the line is gone (step two of leaving);
- who entered the key (the user or the engine), kept in
  ``<data dir>/key_helper.json`` with whether qswap created the settings
  file;
- :meth:`reconcile_key_helper`, the repair ``qswap switch`` runs first: a
  line naming an account that no longer exists (or is not an API-key
  account) is removed, and profiles left with a stale copy get their
  sharing back.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ai_quota_swap import claude_settings, display
from ai_quota_swap.exceptions import KeyHelperUnavailableError, SwitchError
from ai_quota_swap.json_output import account_ref
from ai_quota_swap.locking import FileLock
from ai_quota_swap.printer import accent

KEY_STATE_FILENAME = "key_helper.json"
#: ``qswap run N`` sessions on a key: one JSON line per launch (pid, number).
KEY_RUNS_FILENAME = "key_runs.jsonl"
ENTERED_BY = ("user", "engine")


# -- key_helper.json ------------------------------------------------------------------
#
# While a key is in, the record says which (``number``), who entered it
# (``enteredBy``), since when, whether qswap created the settings file
# (``createdSettings``) and what a blank file held (``blankSettings``).
# Leaving a key keeps a tombstone for it under ``left`` (number -> ``at``,
# ``enteredBy``) instead of deleting the record (decision B5): a line of
# qswap's that turns up again with no record (another program wrote back an
# older copy of the settings) is then known to have come back, and keeps the
# name of whoever had entered it.


# -- what defeats the line (decision B6) ---------------------------------------------
#
# Claude Code obeys these, set in a settings file's ``env`` block, before any
# key line: a credential of its own, or another provider altogether. Named in
# a message, never their values.
DEFEATING_ENV = (
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
)
_PROVIDER_SWITCHES = DEFEATING_ENV[2:]
_SWITCHED_OFF = {"", "0", "false", "no", "off"}
BASE_URL_VAR = "ANTHROPIC_BASE_URL"
_ANTHROPIC_HOSTS = {"api.anthropic.com"}


def settings_object(text: str | None) -> dict:
    """A settings file's object (read past a byte order mark), or ``{}``."""
    if not text:
        return {}
    try:
        obj = json.loads(text.removeprefix("\ufeff"))
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def env_block(obj: dict) -> dict[str, str]:
    """The ``env`` block of a settings object, its values as strings."""
    env = obj.get("env") if isinstance(obj, dict) else None
    if not isinstance(env, dict):
        return {}
    return {str(k): "" if v is None else str(v) for k, v in env.items()}


def defeating_names(env: dict[str, str]) -> list[str]:
    """The variables in ``env`` that Claude Code obeys before a key line."""
    out = []
    for name in DEFEATING_ENV:
        value = (env.get(name) or "").strip()
        if not value:
            continue
        if name in _PROVIDER_SWITCHES and value.lower() in _SWITCHED_OFF:
            continue
        out.append(name)
    return out


def other_host(env: dict[str, str]) -> str | None:
    """The host ``ANTHROPIC_BASE_URL`` sends the key to, when it is not
    Anthropic's API (only the host: a URL can carry a password)."""
    from urllib.parse import urlsplit

    value = (env.get(BASE_URL_VAR) or "").strip()
    if not value:
        return None
    try:
        host = urlsplit(value if "//" in value else f"//{value}").hostname
    except ValueError:
        host = None
    if not host:
        return "a host qswap cannot read from it"
    return None if host.lower() in _ANTHROPIC_HOSTS else host


def _tombstones(record: dict) -> dict:
    left = record.get("left")
    return dict(left) if isinstance(left, dict) else {}


def effective_record(record: object, line_number: str | None) -> dict:
    """key_helper.json as it stands for the line in the settings. From one
    key to another the new record is written before the line with the old
    one under ``replacing``: when the line still names that old key (the
    switch stopped in between), the old record is the one in force, so its
    line is not taken for one that came back."""
    record = record if isinstance(record, dict) else {}
    replacing = record.get("replacing")
    if not isinstance(replacing, dict):
        return record
    rest = {k: v for k, v in record.items() if k != "replacing"}
    if (
        line_number is not None and replacing.get("number") is not None
        and str(replacing["number"]) == line_number
        and str(record.get("number")) != line_number
    ):
        out = {k: v for k, v in replacing.items() if k != "left"}
        left = _tombstones(rest)
        left.pop(line_number, None)
        if left:
            out["left"] = left
        return out
    return rest


def entered_by_of(record: dict, key: str) -> tuple[str, bool]:
    """``(enteredBy, came_back)`` for qswap's line for ``key``, from the
    effective record: its own record's; a tombstone's when the line came
    back after qswap took it out (decision B5); else the user's."""
    entered = str(record["number"]) if record.get("number") is not None else None
    if entered == key and record.get("enteredBy") in ENTERED_BY:
        return record["enteredBy"], False
    tomb = _tombstones(record).get(key)
    if entered != key and isinstance(tomb, dict):
        by = tomb.get("enteredBy")
        return (by if by in ENTERED_BY else "user"), True
    return "user", False


@dataclass(frozen=True)
class KeyFindings:
    """Decision B5: the key line in the user settings, the account list,
    ``key_helper.json``, the run profiles and ``activeAccountNumber``
    compared, and what does not agree. Every switch, list, status, the
    snapshot and doctor read this; :meth:`KeyActivationMixin.reconcile_key_helper`
    repairs from it."""

    state: claude_settings.HelperState
    #: The API-key account active through qswap's line, or None.
    key: str | None = None
    #: The line names an account that cannot give a key: ``{"number", "why"}``.
    problem: dict | None = None
    #: What could not be read, so nothing can be told or repaired:
    #: ``settings`` (the file cannot be parsed or read) or ``accounts``.
    unknown: str | None = None
    #: Who entered the active key (``user`` or ``engine``).
    entered_by: str | None = None
    #: The active key's line came back after qswap took it out.
    came_back: bool = False
    #: The key ``key_helper.json`` records as entered, whose line is gone.
    lost: str | None = None
    #: ``activeAccountNumber`` names a key whose line is gone (and which is
    #: not saved the older way under the login).
    stale_active: str | None = None
    #: Run profiles still sharing the user settings while the line is in.
    linked: tuple[Path, ...] = ()
    #: Run profiles holding a settings copy while no line is in.
    copies: tuple[Path, ...] = ()
    #: The settings cannot be parsed while ``key_helper.json`` names a key.
    unreadable_record: str | None = None


def assess_key_state(
    state: claude_settings.HelperState,
    accounts: dict | None,
    record: dict | None,
    *,
    active_number: object = None,
    login_slot: str | None = None,
    linked: tuple = (),
    copies: tuple = (),
) -> KeyFindings:
    """The comparison itself, from what was read (``accounts`` None: the
    account list could not be read). No I/O: doctor reads the same files
    through its probes and calls this too."""
    record = record if isinstance(record, dict) else {}
    if state.kind == claude_settings.INVALID:
        entered = str(record["number"]) if record.get("number") is not None else None
        return KeyFindings(state, unknown="settings", unreadable_record=entered)
    if accounts is None:
        return KeyFindings(state, unknown="accounts")
    record = effective_record(record, state.number if state.ours else None)
    entered = str(record["number"]) if record.get("number") is not None else None
    key = problem = None
    line_number = state.number if state.ours else None
    if line_number is not None:
        row = accounts.get(line_number)
        if not isinstance(row, dict):
            problem = {"number": line_number, "why": "missing"}
        elif row.get("kind") != "api_key":
            problem = {"number": line_number, "why": "not-api-key"}
        else:
            key = line_number
    entered_by, came_back = None, False
    if key is not None:
        entered_by, came_back = entered_by_of(record, key)
    lost = entered if entered is not None and entered != line_number else None
    stale_active = None
    if active_number is not None:
        active = str(active_number)
        row = accounts.get(active)
        # Only a key qswap has a trace of entering through the line (a
        # record or a tombstone): a number an import seeded is left alone.
        traced = active == entered or active in _tombstones(record)
        if (
            traced and isinstance(row, dict) and row.get("kind") == "api_key"
            and active != line_number and active != login_slot
        ):
            stale_active = active
    return KeyFindings(
        state, key=key, problem=problem, entered_by=entered_by, came_back=came_back,
        lost=lost, stale_active=stale_active,
        linked=tuple(linked) if key is not None else (),
        copies=tuple(copies) if line_number is None else (),
    )


class KeyActivationMixin:
    """See the module docstring. Mixed into the Claude Code switcher."""

    # -- state ------------------------------------------------------------------

    def key_helper_state(self) -> claude_settings.HelperState:
        """What Claude Code's user settings say about ``apiKeyHelper``."""
        return claude_settings.read_state()

    @property
    def key_state_path(self) -> Path:
        return Path(self.backup_dir) / KEY_STATE_FILENAME

    def _key_state(self) -> dict:
        try:
            data = json.loads(self.key_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_key_state(self, state: dict | None) -> None:
        from ai_quota_swap.settings import atomic_write_json

        try:
            if state is None:
                self.key_state_path.unlink(missing_ok=True)
            else:
                atomic_write_json(self.key_state_path, state)
        except OSError as e:
            self._logger.warning(f"Could not record the key helper state: {e}")

    def _helper_verdict(
        self, state: claude_settings.HelperState | None = None, data: dict | None = None
    ) -> tuple[str | None, dict | None]:
        """``(number, problem)``: the API-key account qswap's line makes
        active, or a problem ``{"number", "why"}`` when the line names an
        account that does not exist (``missing``) or is not an API-key
        account (``not-api-key``)."""
        state = self.key_helper_state() if state is None else state
        if not state.ours or state.number is None:
            return None, None
        if data is None:
            # A list that cannot be read raises: "unknown", never "missing"
            # (decision B5), so no caller repairs or reports on it.
            data = self._get_sequence_data() or {}
        record = (data.get("accounts") or {}).get(state.number)
        if not isinstance(record, dict):
            return None, {"number": state.number, "why": "missing"}
        if record.get("kind") != "api_key":
            return None, {"number": state.number, "why": "not-api-key"}
        return state.number, None

    def helper_account(self) -> str | None:
        """The API-key account active through qswap's helper line, or None."""
        try:
            return self._helper_verdict()[0]
        except Exception as e:
            self._logger.debug(f"Could not read the key helper state: {e!r}")
            return None

    def key_line_number(self) -> str | None:
        """The account qswap's key line names, for the commands that must not
        renumber or overwrite it: read from the settings, or from
        key_helper.json when the settings cannot be parsed (the line may
        still be there). None when there is no line of qswap's."""
        try:
            state = self.key_helper_state()
        except Exception:
            return None
        if state.ours:
            return state.number
        if state.kind == claude_settings.INVALID:
            number = self._key_state().get("number")
            return str(number) if number is not None else None
        return None

    def key_line_unreadable(self) -> list[str] | None:
        """The one note every surface (and the guards, and a refused switch)
        shows while the settings cannot be parsed and key_helper.json names a
        key (decision B5, F52): the file cannot be read, qswap last made #N
        active there, and fixing the file is the next step. None otherwise."""
        try:
            state = self.key_helper_state()
            if state.kind != claude_settings.INVALID:
                return None
            number = self._key_state().get("number")
            if number is None:
                return None
            label = self.label(str(number), long=False)
        except Exception:
            return None
        return display.key_settings_unreadable(state.shown_path(), state.error, label)

    def key_findings(self) -> KeyFindings:
        """The line, the account list, key_helper.json, the run profiles and
        ``activeAccountNumber`` compared (:func:`assess_key_state`). Reads
        only; never raises."""
        state = self.key_helper_state()
        accounts: dict | None
        active = login = None
        try:
            data = self._get_sequence_data() or {}
            accounts = data.get("accounts") or {}
            active = data.get("activeAccountNumber")
        except Exception:
            accounts = None
        if (
            accounts is not None and active is not None
            and (accounts.get(str(active)) or {}).get("kind") == "api_key"
            and not (state.ours and state.number == str(active))
        ):
            # Only then does it matter whether the key is saved the older
            # way under the login (read only when needed).
            try:
                login = self.login_account_number()
            except Exception:
                login = None
        linked: tuple = ()
        copies: tuple = ()
        if accounts is not None and state.kind != claude_settings.INVALID:
            if state.ours:
                linked = tuple(self.profiles_still_linked())
            else:
                copies = tuple(self.stale_profile_copies())
        return assess_key_state(
            state, accounts, self._key_state(), active_number=active, login_slot=login,
            linked=linked, copies=copies,
        )

    def helper_problem(self) -> dict | None:
        """A helper line naming an account that cannot give a key, or None."""
        try:
            return self._helper_verdict()[1]
        except Exception:
            return None

    def login_account_number(self) -> str | None:
        """The slot of the live login (``~/.claude.json``); None when there
        is none or it is unmanaged. Under an active key this is the account
        underneath. Deliberately no fallback to ``activeAccountNumber``."""
        identity = self._get_current_account()
        if identity is None:
            return None
        data = self._get_sequence_data() or {}
        email, org_uuid = identity
        return self._find_account_slot(data, email, org_uuid)

    def current_account_number(self) -> str | None:
        """The active account: the API-key account whose helper line is in
        Claude Code's settings, else the slot of the live login (None when
        there is none or it is unmanaged; never a guess from
        ``activeAccountNumber``, so the engine cannot act on the wrong
        account). :meth:`has_live_login` tells the two None cases apart."""
        helper = self.helper_account()
        if helper is not None:
            return helper
        return self.login_account_number()

    def underneath_account_number(self) -> str | None:
        """The subscription logged in under an active key; None otherwise."""
        key = self.helper_account()
        if key is None:
            return None
        under = self.login_account_number()
        if under is None or under == key or self._account_kind(under) == "api_key":
            return None
        return under

    def key_entered_by(self) -> str | None:
        """Who entered the active key: ``user`` or ``engine``; None when no
        key is active through the helper. A line that came back after qswap
        took it out (no record of its own, a tombstone for its number) keeps the name of
        whoever had entered it (decision B5). A line qswap has no trace of
        (written by an older run, or by hand) counts as the user's: such a
        key is only left at its budget or by the user."""
        number = self.helper_account()
        if number is None:
            return None
        # The same rule list, status and doctor read (assess_key_state).
        return entered_by_of(effective_record(self._key_state(), number), number)[0]

    def _adopt_engine_key(self, number: str) -> str | None:
        """The user asked for the key the engine entered: record the choice,
        so auto-switch keeps it until its budget or credit. Returns the
        sentence that says so, or None when nothing changed."""
        state = self._key_state()
        if str(state.get("number")) != str(number) or state.get("enteredBy") != "engine":
            if state.get("number") is None and self.key_entered_by() == "engine":
                # Came back with the engine's name: the record is made anew.
                with FileLock(self.lock_file):
                    self._write_key_state(self._entered_record(state, str(number), "user"))
                return display.KEY_KEPT
            return None
        with FileLock(self.lock_file):
            self._write_key_state({**state, "enteredBy": "user"})
        return display.KEY_KEPT

    def repair_active_key(self, number: str, *, entered_by: str = "user") -> list[str]:
        """Decision B2: ``qswap switch N`` to the key that is already active
        checks its line the way Claude Code runs it
        (:func:`claude_settings.line_answers`) and, when it does not answer,
        writes it again for this install; a missing record is written (by
        ``entered_by``), and run profiles still linked get their copy.
        Returns what was done, in words. Raises :class:`SwitchError` when the
        line cannot answer even so: the saved key's own refusal when the key
        is gone, else the line's reason and the check to run."""
        number = str(number)
        done: list[str] = []
        label = self.label(number, long=False)
        why = claude_settings.line_answers(self.key_helper_state())
        if why is not None:
            row = ((self._get_sequence_data() or {}).get("accounts") or {}).get(number) or {}
            self._require_saved_key(number, row.get("email", ""))
            with FileLock(self.lock_file):
                try:
                    claude_settings.install(number)
                except claude_settings.SettingsError as e:
                    path = claude_settings.shown_path(e.path or claude_settings.settings_path())
                    raise SwitchError(
                        f"The key line for {label} {why}, and it could not be written "
                        f"again: {path} {e.reason}."
                    ) from e
            again = claude_settings.line_answers(self.key_helper_state())
            if again is not None:
                raise SwitchError(
                    f"The key line for {label} {again}, also after it was written "
                    f"again for this qswap. Check it with: qswap key-helper {number} --check"
                )
            self._logger.info(f"Wrote the key line for account {number} again: it {why}")
            done.append(f"Wrote the key line for {label} again: it {why}.")
        state = self._key_state()
        if str(state.get("number")) != number:
            with FileLock(self.lock_file):
                self._write_key_state(self._entered_record(
                    state, number, entered_by, path=claude_settings.settings_path(),
                ))
        if self.profiles_still_linked():
            with FileLock(self.lock_file):
                still = self.isolate_run_profiles()
            if still:
                raise SwitchError(self._profiles_unisolated_reason(still).capitalize() + ".")
        return done

    def _settings_env(self) -> dict[str, str]:
        """The ``env`` block of the user settings (``{}`` when there is none
        or the file cannot be read)."""
        try:
            raw = claude_settings.settings_path().read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return {}
        return env_block(settings_object(raw))

    def engine_watching(self) -> bool:
        """Whether an auto-switch engine is running for this data directory
        (its loop's heartbeat is current): one is watching the limit of a key
        it entered (decision B8). Never raises."""
        try:
            from ai_quota_swap.autoswitch import engine_running, read_heartbeat

            return engine_running(read_heartbeat(Path(self.backup_dir)), time.time())
        except Exception:
            return False

    def key_way_back(self) -> str | None:
        """The command that takes sessions off the active key, for the
        surfaces that say nothing watches it (decisions B8, A16), the same in
        each: the switch to the login underneath; with a login qswap does not
        manage underneath, adding it (a switch to a subscription would
        overwrite it, and ``qswap add`` then names the switch); with no login
        at all, the switch to the first subscription, which logs it in. None
        when there is no subscription either. Never raises."""
        try:
            login = self.login_account_number()
            if login is not None and self._account_kind(login) != "api_key":
                return f"qswap switch {login}"
            if login is None and self.has_live_login():
                return "qswap add"
            first = next(
                (
                    n for n in self.switchable_account_numbers()
                    if self.account_kind_for(n) != "api_key"
                ),
                None,
            )
            return f"qswap switch {first}" if first is not None else None
        except Exception:
            return None

    def key_helper_available(self) -> bool:
        """Whether qswap can install its helper line now and sessions would
        follow it: no other helper, a file it can parse, and no variable in
        the file's ``env`` block that Claude Code obeys before the line. The
        engine reads this before it enters a key (key-helper-unavailable)."""
        try:
            return self.key_helper_state().installable and not defeating_names(
                self._settings_env()
            )
        except Exception:
            return False

    def helper_fix_command(self) -> str:
        """The command that removes a helper line that cannot give a key, or
        restores stale profile copies: a switch to the live login."""
        login = None
        try:
            login = self.login_account_number()
        except Exception:
            pass
        return f"qswap switch {login}" if login else "qswap switch N"

    # -- qswap run on a key ---------------------------------------------------------

    def record_key_run(self, number: str, pid: int) -> None:
        """Remember a ``qswap run N`` session on a key (its pid survives the
        exec into claude), so removing the account can name it. Never raises."""
        try:
            path = Path(self.backup_dir) / KEY_RUNS_FILENAME
            line = json.dumps({"pid": int(pid), "number": str(number), "ts": time.time()})
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            self._logger.debug(f"Could not record the key run: {e!r}")

    def key_run_pids(self, number: str) -> list[int]:
        """The live ``qswap run N`` sessions on account N's key."""
        from ai_quota_swap.process_detection import is_pid_alive

        pids: list[int] = []
        try:
            lines = (Path(self.backup_dir) / KEY_RUNS_FILENAME).read_text(
                encoding="utf-8"
            ).splitlines()
        except OSError:
            return pids
        for raw in lines:
            try:
                row = json.loads(raw)
                pid = int(row["pid"])
            except (ValueError, KeyError, TypeError):
                continue
            if str(row.get("number")) == str(number) and pid not in pids and is_pid_alive(pid):
                pids.append(pid)
        return pids

    # -- run profiles --------------------------------------------------------------

    def _profiles_share_user_settings(self) -> bool:
        """Whether run profiles share the settings file the line goes into
        (they link ``~/.claude/settings.json``; a ``CLAUDE_CONFIG_DIR`` that
        points elsewhere moves the user settings away from them)."""
        try:
            mine = os.path.realpath(claude_settings.settings_path())
            theirs = os.path.realpath(Path.home() / ".claude" / "settings.json")
        except OSError:
            return True
        return os.path.normcase(mine) == os.path.normcase(theirs)

    def _session_manager(self):
        from ai_quota_swap.session import SessionManager

        return SessionManager(self)

    def isolate_run_profiles(self) -> list[Path] | None:
        """Give run profiles their settings copy (step one of entering a
        key). Returns the profiles still linked to the user settings, which a
        line written now would reach; None when isolation itself failed."""
        if not self._profiles_share_user_settings():
            return []
        try:
            manager = self._session_manager()
            manager.isolate_settings()
            return manager.settings_still_shared()
        except Exception as e:
            self._logger.warning(f"Could not give run profiles a settings copy: {e!r}")
            return None

    @staticmethod
    def _profiles_unisolated_reason(shared: list[Path] | None) -> str:
        if not shared:
            return (
                "qswap run profiles could not be given their own copy of the "
                "settings, so sessions there would use the key too"
            )
        if len(shared) == 1:
            return (
                "a qswap run profile could not be given its own copy of the "
                "settings, so sessions there would use the key too"
            )
        return (
            f"{len(shared)} qswap run profiles could not be given their own copy "
            "of the settings, so sessions there would use the key too"
        )

    def restore_run_profiles(self, *, force: bool = False) -> int:
        try:
            return self._session_manager().restore_settings(force=force)
        except Exception as e:
            self._logger.warning(f"Could not restore settings sharing in run profiles: {e!r}")
            return 0

    def stale_profile_copies(self) -> list[Path]:
        try:
            return self._session_manager().stale_settings_copies()
        except Exception:
            return []

    def profiles_still_linked(self) -> list[Path]:
        """Run profiles whose settings still lead to the user settings while
        qswap's line is in them (F9): their sessions follow the key."""
        if not self._profiles_share_user_settings():
            return []
        try:
            return self._session_manager().settings_still_shared()
        except Exception:
            return []

    # -- entering and leaving a key -----------------------------------------------

    def _key_unavailable(
        self, number: str, reason: str, *, kind: str | None = None,
        names: tuple[str, ...] = (), log_out_sessions: bool = False,
    ) -> KeyHelperUnavailableError:
        """The refusal of a switch to the key ``number`` (decision B6):
        ``kind`` is what stopped it. Another helper (``foreign``) and a
        variable in the env block (``env``) are refused with the one way
        forward, removing it; anything else offers ``--log-out-sessions``
        after the fix that keeps everyone logged in."""
        path = claude_settings.shown_path(claude_settings.settings_path())
        label = self.label(number, long=False)
        if kind in (claude_settings.FOREIGN, "env"):
            text = (
                display.key_helper_foreign_refusal(
                    label, number, path, log_out_sessions=log_out_sessions,
                )
                if kind == claude_settings.FOREIGN
                else display.key_line_defeated_refusal(label, number, path, names)
            )
            return KeyHelperUnavailableError(
                text, number=number, reason=reason, log_out_sessions=False,
            )
        fix = None
        if kind == claude_settings.INVALID:
            fix = f"Fix the JSON in {path}"
        elif kind == "write":
            fix = f"Make {path} writable"
        sessions, unreadable = self.shared_login_scan()
        current = self.login_account_number()
        from_key = (
            self.label(current, long=False)
            if current and current != str(number) and self._account_kind(current) == "api_key"
            else None
        )
        records = self.shared_login_unreadable() if unreadable else []
        return KeyHelperUnavailableError(
            display.key_helper_refusal(
                label, number, reason,
                display.blocking_sessions(len(sessions), unreadable), from_key=from_key,
                records=records, fix=fix,
            ),
            number=number, reason=reason, sessions=len(sessions), unreadable=unreadable,
            from_key=from_key is not None, records=records,
        )

    def _require_saved_key(self, number: str, email: str) -> None:
        self._heal_api_key_backup(number, email)
        stored, unreadable = self._read_account_credentials_ex(number, email)
        from ai_quota_swap.key_helper import is_key

        if is_key(stored):
            return  # what key-helper will print
        if stored:
            # The line would run a helper that prints no key, and every
            # Claude Code process reading the settings would lose its auth.
            raise SwitchError(
                f"{self.label(number, long=False)}'s saved credential is not an API "
                f"key. Save the key again with: qswap add-token KEY --slot {number}"
            )
        if unreadable:
            raise SwitchError(
                f"{self.label(number, long=False)}'s saved key is in the macOS Keychain "
                "but it is unreadable right now (locked or no GUI session). Unlock "
                "it and retry."
            )
        raise SwitchError(
            f"{self.label(number, long=False)} has no saved key. Save one with: "
            f"qswap add-token KEY --slot {number}"
        )

    def _enter_key(
        self, number: str, *, entered_by: str = "user", emit_output: bool = True
    ) -> dict:
        """Make the API-key account ``number`` active through the helper line.

        Step one gives every run profile that shares the user settings a copy
        without the line; step two installs the line. A failure before the
        line is written puts the profiles back. Writes nothing to the login
        item, the saved-key item or ``~/.claude.json``. Raises
        :class:`KeyHelperUnavailableError` when the line cannot be installed.
        """
        self._refuse_session_shell()
        number = str(number)
        data = self._get_sequence_data() or {}
        record = (data.get("accounts") or {}).get(number) or {}
        email = record.get("email", "")
        self._require_saved_key(number, email)
        state = self.key_helper_state()
        if not state.installable:
            raise self._key_unavailable(
                number, f"{state.shown_path()} {state.reason() or 'cannot be changed'}",
                kind=state.kind,
            )
        env = self._settings_env()
        names = tuple(defeating_names(env))
        if names:
            raise self._key_unavailable(
                number,
                f"{state.shown_path()} sets {', '.join(names)} in its env block",
                kind="env", names=names,
            )
        host = other_host(env)
        previous_key = self.helper_account()
        underneath = self.login_account_number()
        source = previous_key or underneath
        if underneath == number or (
            underneath is not None and self._account_kind(underneath) == "api_key"
        ):
            # A key saved the older way (--log-out-sessions) is in the login's
            # place: no subscription is logged in underneath.
            underneath = None
        unmanaged = None
        if underneath is None and source is None:
            identity = self._get_current_account()
            if identity is not None and identity[0]:
                unmanaged = identity[0]  # a login qswap does not manage
        with FileLock(self.lock_file):
            shared = self.isolate_run_profiles()
            if shared is None or shared:
                # Fail closed: a profile still linked would follow the line.
                if previous_key is None:
                    self.restore_run_profiles(force=True)
                raise self._key_unavailable(number, self._profiles_unisolated_reason(shared))
            old = self._key_state()
            blank = claude_settings.blank_text(state.path) if previous_key is None else None
            if previous_key is not None and isinstance(old.get("blankSettings"), str):
                blank = old["blankSettings"]
            # The brace's line the line splits (a first member on it), joined
            # again when the line comes out (decision B4).
            lead = claude_settings.brace_lead(state.path) if previous_key is None else None
            if previous_key is not None and isinstance(old.get("braceLead"), str):
                lead = old["braceLead"]
            created = state.kind == claude_settings.NO_FILE or bool(
                previous_key is not None and old.get("createdSettings")
            )
            key_state = self._entered_record(
                old, number, entered_by, created=created, path=state.path, blank=blank,
                lead=lead,
            )
            # The record goes in before the line (F13): a crash after the
            # line leaves a record naming it, so a key the engine entered
            # stays the engine's; a crash before it leaves a record with no
            # line, which every surface reports and the next switch clears.
            # From one key to another the old record rides along (under
            # ``replacing``, its key not yet tombstoned) until the line is
            # replaced: a stop in between leaves the old line with the record
            # that names it, not a line that seems to have come back.
            pending = key_state
            if previous_key is not None and str(old.get("number")) == previous_key:
                pending = dict(key_state)
                left = _tombstones(pending)
                left.pop(previous_key, None)
                pending.pop("left", None)
                if left:
                    pending["left"] = left
                pending["replacing"] = {k: v for k, v in old.items() if k != "left"}
            self._write_key_state(pending)
            try:
                created_now, _ = claude_settings.install(number)
            except claude_settings.SettingsError as e:
                self._write_key_state(old or None)
                if previous_key is None:
                    self.restore_run_profiles(force=True)
                raise self._key_unavailable(
                    number, f"{claude_settings.shown_path(e.path or state.path)} {e.reason}",
                    kind=e.kind,
                ) from e
            if created_now and not key_state["createdSettings"]:
                key_state["createdSettings"] = True
            if pending is not key_state or created_now:
                self._write_key_state(key_state)
            # A qswap run launch between the two steps saw no line yet and
            # may have put its link back: a second pass, now that launches
            # see the line and copy by themselves.
            shared = self.isolate_run_profiles()
            if shared is None or shared:
                self._undo_install(previous_key, created_now, blank, lead)
                self._write_key_state(old or None)
                raise self._key_unavailable(number, self._profiles_unisolated_reason(shared))
            self._record_active_number(number)
        self._logger.info(
            f"Activated account {number} through the key helper "
            f"(entered by {entered_by}; from {source or 'none'})"
        )
        self._record_activation(number, email, record.get("organizationUuid") or "", how="helper")
        from_ref = None
        if source is not None:
            from_ref = account_ref(int(source), self.account_email(source))
        followup = display.key_switch_followup(
            target=self.label(number, long=False),
            underneath=self.label(underneath, long=False) if underneath else unmanaged,
            from_key=self.label(previous_key, long=False) if previous_key else None,
            back=f"qswap switch {underneath}" if underneath else None,
        )
        sent = (
            [display.key_sent_elsewhere(state.shown_path(), host, self.label(number, long=False))]
            if host else []
        )
        if emit_output:
            where = f"{self.label(source, long=False)} → " if source else "to "
            print(f"{accent('Switched')} {where}{self._landed_label(number, email)}")
            for line in followup:
                print(line)
            from ai_quota_swap.printer import warning

            for line in sent:
                warning(line)
        op = self._op_result(
            from_ref, account_ref(int(number), email),
            [line.removeprefix("Warning: ") for line in sent], followup,
        )
        op["how"] = display.HOW_HELPER
        return op

    @staticmethod
    def _entered_record(
        old: dict, number: str, entered_by: str, *, created: bool = False,
        path: Path | None = None, blank: str | None = None, lead: str | None = None,
    ) -> dict:
        """key_helper.json once ``number`` is entered: its record, the
        tombstones kept, the key left on the way (key to key) tombstoned and
        ``number``'s own tombstone dropped."""
        left = _tombstones(old)
        previous = old.get("number")
        if previous is not None and str(previous) != number:
            by = old.get("enteredBy")
            left[str(previous)] = {
                "at": time.time(), "enteredBy": by if by in ENTERED_BY else "user",
            }
        left.pop(number, None)
        record: dict = {
            "number": number,
            "enteredBy": entered_by if entered_by in ENTERED_BY else "user",
            "since": time.time(),
            "createdSettings": bool(created),
            "settingsPath": str(path if path is not None else claude_settings.settings_path()),
        }
        if blank is not None:
            # The file was there but held no settings: leaving puts that back.
            record["blankSettings"] = blank
        if lead is not None:
            # The line split the brace's line: leaving joins it again.
            record["braceLead"] = lead
        if left:
            record["left"] = left
        return record

    @staticmethod
    def _left_record(old: dict, number: str | None) -> dict | None:
        """key_helper.json once the key is left: no record, a tombstone for
        ``number`` (the line taken out) and for the key the record named.
        None when nothing is left to keep."""
        left = _tombstones(old)
        entered = str(old["number"]) if old.get("number") is not None else None
        for n in {entered, number} - {None}:
            if n == entered:
                by = old.get("enteredBy")
            else:
                by = (left.get(n) or {}).get("enteredBy")
            left[n] = {"at": time.time(), "enteredBy": by if by in ENTERED_BY else "user"}
        return {"left": left} if left else None

    def forget_key(self, number: str) -> None:
        """Drop the record and tombstone of ``number`` (the account is being
        removed): a line for that number later is nobody's. Never raises."""
        try:
            state = self._key_state()
            left = _tombstones(state)
            changed = left.pop(str(number), None) is not None
            if str(state.get("number")) == str(number):
                state = {}
                changed = True
            if not changed:
                return
            state = {k: v for k, v in state.items() if k != "left"}
            if left:
                state["left"] = left
            self._write_key_state(state or None)
        except Exception as e:
            self._logger.debug(f"Could not forget the key record of {number}: {e!r}")

    def _undo_install(
        self, previous_key: str | None, created: bool, blank: str | None = None,
        lead: str | None = None,
    ) -> None:
        """Take back a line just installed: the previous key's line again, or
        no line (and the file qswap created removed) with the profiles put
        back. Logged, never raised: the refusal that follows says why."""
        try:
            if previous_key is not None:
                claude_settings.install(previous_key)
                return
            claude_settings.remove(
                remove_file_if_empty=bool(created), restore_blank=blank, restore_lead=lead,
            )
            self.restore_run_profiles(force=True)
        except Exception as e:
            self._logger.warning(f"Could not take back the key line: {e!r}")

    def _record_active_number(self, number: str) -> None:
        """``activeAccountNumber`` in ``sequence.json``: recorded, never what
        decides the active account, but read as a fallback (a fresh machine,
        plain rotation) and by scripts. The caller holds the account lock."""
        from ai_quota_swap.models import get_timestamp

        try:
            data = self._get_sequence_data() or {}
            if data.get("activeAccountNumber") == int(number):
                return
            data["activeAccountNumber"] = int(number)
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)
        except Exception as e:
            self._logger.warning(f"Could not record the active account: {e!r}")

    def _active_number_back(self, number: str | None) -> None:
        """``activeAccountNumber`` once the line for ``number`` is out: the
        live login's slot when it named the key, or no number at all (decision
        B5, F25). The caller holds the account lock."""
        if number is None:
            return
        from ai_quota_swap.models import get_timestamp

        try:
            data = self._get_sequence_data() or {}
            if data.get("activeAccountNumber") != int(number):
                return
            login = self.login_account_number()
            data["activeAccountNumber"] = int(login) if login else None
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)
        except Exception as e:
            self._logger.warning(f"Could not record the active account: {e!r}")

    def _leave_key(self, *, tombstone: bool = True) -> bool:
        """Remove qswap's helper line (any line of qswap's, valid or not),
        then put the run profiles' settings sharing back. key_helper.json
        keeps a tombstone for the key (``tombstone``), and
        ``activeAccountNumber`` goes back to the live login. Returns whether a
        line was removed. Raises :class:`SwitchError` when the settings file
        cannot be parsed, so the line cannot be found."""
        self._refuse_session_shell()
        line = self.key_helper_state()
        number = line.number if line.ours else None
        state = effective_record(self._key_state(), number)
        with FileLock(self.lock_file):
            try:
                blank = state.get("blankSettings")
                lead = state.get("braceLead")
                removed = claude_settings.remove(
                    remove_file_if_empty=bool(state.get("createdSettings")),
                    restore_blank=blank if isinstance(blank, str) else None,
                    restore_lead=lead if isinstance(lead, str) else None,
                )
            except claude_settings.SettingsError as e:
                path = claude_settings.shown_path(e.path or claude_settings.settings_path())
                raise SwitchError(
                    f"The key line cannot be removed: {path} {e.reason}. Fix the "
                    'file, or delete its "apiKeyHelper" line by hand, then retry.'
                ) from e
            # Without a tombstone for the line taken out (one naming an
            # account that cannot give a key), the other keys' stay.
            left = self._left_record(state, number if tombstone else None)
            if not tombstone and left is not None and number is not None:
                stones = _tombstones(left)
                stones.pop(number, None)
                left = {"left": stones} if stones else None
            self._write_key_state(left)
            self.restore_run_profiles()
            self._active_number_back(number or (
                str(state["number"]) if state.get("number") is not None else None
            ))
        if removed:
            self._logger.info("Removed the key helper line from Claude Code's settings")
        return removed

    def _active_number(self) -> object:
        """``activeAccountNumber`` as it stands (None when it cannot be read),
        for a put-back that restores it exactly."""
        try:
            return (self._get_sequence_data() or {}).get("activeAccountNumber")
        except Exception:
            return None

    def _restore_key(
        self, number: str, state: dict, cause: BaseException, active: object = ...,
    ) -> None:
        """Put the helper line for ``number`` back after the switch that
        followed its removal failed, so the failed switch moves no session.
        ``state`` is key_helper.json as it stood before. Reads no Keychain
        (a locked one may be the cause). Raises :class:`SwitchError` naming
        both failures when the line cannot be put back; the run profiles
        given a copy on the way are then given their sharing back, since no
        line is in."""
        try:
            with FileLock(self.lock_file):
                shared = self.isolate_run_profiles()
                if shared is None or shared:
                    self.restore_run_profiles(force=True)
                    raise OSError(self._profiles_unisolated_reason(shared))
                try:
                    claude_settings.install(number)
                except Exception:
                    self.restore_run_profiles(force=True)
                    raise
                self._write_key_state(state or None)
                if active is not ...:
                    self._put_active_number(active)
            self._logger.info(f"Put the key line for account {number} back after: {cause}")
        except Exception as e:
            under = self.login_account_number()
            where = self.label(under, long=False) if under else "the live login"
            what = str(cause) or "The switch was interrupted."
            raise SwitchError(
                f"{what} {self.label(number, long=False)} was left, and its key line "
                f"could not be put back ({e}): running sessions are on {where}. To go "
                f"back to the key: qswap switch {number}"
            ) from cause

    def _put_active_number(self, value: object) -> None:
        """``activeAccountNumber`` set back to exactly ``value``. The caller
        holds the account lock."""
        try:
            data = self._get_sequence_data() or {}
            if data.get("activeAccountNumber") != value:
                data["activeAccountNumber"] = value
                self._write_json(self.sequence_file, data)
        except Exception as e:
            self._logger.warning(f"Could not record the active account: {e!r}")

    def leave_key_for_unmanaged_login(self) -> dict | None:
        """Leave the active key when the login underneath is one qswap does
        not manage: remove the line only, so running sessions return to that
        login. For the engine at a key's budget or credit, when no managed
        subscription can take over. ``{"from", "to"}`` refs (``to`` has no
        number), or None when there is no such login (none at all, or a
        managed one, which the ordinary switch handles) and nothing changed."""
        key = self.helper_account()
        if key is None:
            return None
        identity = self._get_current_account()
        if identity is None or not identity[0] or self.login_account_number() is not None:
            return None
        key_email = self.account_email(key)
        self._leave_key()
        self._note_live_login(None, identity)
        self._logger.info(f"Left account {key} for the unmanaged login underneath")
        return {
            "switched": True,
            "from": account_ref(int(key), key_email),
            "to": {"number": None, "email": identity[0]},
            "warnings": [],
        }

    def reconcile_key_helper(self) -> list[str]:
        """Decision B5: the one repair, run first by every switch. From
        :meth:`key_findings`: a line that runs a qswap no longer there is
        pointed at this one; a line naming an account that cannot give a key
        is removed; a record whose line is gone becomes a tombstone; an
        ``activeAccountNumber`` naming a key whose line is gone goes back to
        the live login; run profiles still linked under the line get their
        copy, and copies left with no line get their sharing back. Nothing is
        repaired while the settings or the account list cannot be read.
        Returns what was done, in words. Never raises."""
        done: list[str] = []
        try:
            f = self.key_findings()
            if f.unknown is not None:
                return done
            state = f.state
            stored = self._key_state()
            if "replacing" in stored:
                # A switch from one key to another stopped between its record
                # and its line: the record in force is written down plainly.
                with FileLock(self.lock_file):
                    stored = self._key_state()
                    if "replacing" in stored:
                        self._write_key_state(
                            effective_record(stored, state.number if state.ours else None)
                            or None
                        )
            if f.key is not None and state.stale_command:
                # qswap moved (a reinstall): the line runs a command that is
                # gone. The same line, pointing at this qswap.
                claude_settings.install(f.key)
                done.append(
                    f"Updated the key line for #{f.key} to run qswap from its new place."
                )
            if f.problem is not None:
                self._leave_key(tombstone=False)
                done.append(
                    f"Removed the key line for #{f.problem['number']} from Claude Code's "
                    "settings."
                )
            elif f.lost is not None:
                with FileLock(self.lock_file):
                    # Read again under the lock: a switch writes its record
                    # and then its line while holding it, and a record caught
                    # in between is not lost.
                    if self.key_findings().lost == f.lost:
                        self._write_key_state(self._left_record(self._key_state(), None))
                        done.append(
                            f"Cleared the record of #{f.lost} as the active API key: its "
                            "key line is no longer in Claude Code's settings."
                        )
            if f.stale_active is not None:
                with FileLock(self.lock_file):
                    if self.key_findings().stale_active == f.stale_active:
                        self._active_number_back(f.stale_active)
            if f.linked:
                with FileLock(self.lock_file):
                    still = self.isolate_run_profiles()
                given = len(f.linked) - len(still or ()) if still is not None else 0
                if given > 0:
                    noun = "profile" if given == 1 else "profiles"
                    done.append(
                        f"Gave {given} qswap run {noun} its own copy of the settings."
                    )
            stale = self.stale_profile_copies() if f.problem is not None else f.copies
            if stale:
                # Under the lock: an entry gives profiles their copies and then
                # writes the line while holding it (restore_settings checks for
                # the line itself).
                with FileLock(self.lock_file):
                    restored = self.restore_run_profiles()
                if restored:
                    noun = "profile" if restored == 1 else "profiles"
                    done.append(f"Restored the shared settings in {restored} qswap run {noun}.")
        except Exception as e:
            self._logger.warning(f"Key helper repair failed: {e!r}")
        return done
