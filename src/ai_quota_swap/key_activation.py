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
            try:
                data = self._get_sequence_data() or {}
            except Exception:
                data = {}
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
        key is active through the helper. A line qswap has no record of
        (written by an older run, or by hand) counts as the user's: such a
        key is only left at its budget or by the user."""
        number = self.helper_account()
        if number is None:
            return None
        state = self._key_state()
        if str(state.get("number")) == number and state.get("enteredBy") in ENTERED_BY:
            return state["enteredBy"]
        return "user"

    def key_helper_available(self) -> bool:
        """Whether qswap can install its helper line now (no other helper,
        and a file it can parse)."""
        try:
            return self.key_helper_state().installable
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

    def isolate_run_profiles(self) -> int:
        if not self._profiles_share_user_settings():
            return 0
        try:
            return self._session_manager().isolate_settings()
        except Exception as e:
            self._logger.warning(f"Could not give run profiles a settings copy: {e!r}")
            return 0

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

    # -- entering and leaving a key -----------------------------------------------

    def _key_unavailable(self, number: str, reason: str) -> KeyHelperUnavailableError:
        sessions, unreadable = self.shared_login_scan()
        current = self.login_account_number()
        from_key = (
            self.label(current, long=False)
            if current and current != str(number) and self._account_kind(current) == "api_key"
            else None
        )
        return KeyHelperUnavailableError(
            display.key_helper_refusal(
                self.label(number, long=False), number, reason,
                display.blocking_sessions(len(sessions), unreadable), from_key=from_key,
            ),
            number=number, reason=reason, sessions=len(sessions), unreadable=unreadable,
            from_key=from_key is not None,
        )

    def _require_saved_key(self, number: str, email: str) -> None:
        self._heal_api_key_backup(number, email)
        stored, unreadable = self._read_account_credentials_ex(number, email)
        if stored:
            # What key-helper will print (it says so itself when the saved
            # credential is not a key; nothing here can fix that).
            return
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
                number, f"{state.shown_path()} {state.reason() or 'cannot be changed'}"
            )
        previous_key = self.helper_account()
        underneath = self.login_account_number()
        source = previous_key or underneath
        if underneath == number or (
            underneath is not None and self._account_kind(underneath) == "api_key"
        ):
            # A key saved the older way (--log-out-sessions) is in the login's
            # place: no subscription is logged in underneath.
            underneath = None
        with FileLock(self.lock_file):
            self.isolate_run_profiles()
            try:
                created, _ = claude_settings.install(number)
            except claude_settings.SettingsError as e:
                if previous_key is None:
                    self.restore_run_profiles(force=True)
                raise self._key_unavailable(
                    number, f"{claude_settings.shown_path(e.path or state.path)} {e.reason}"
                ) from e
            old = self._key_state()
            created = bool(created or (previous_key is not None and old.get("createdSettings")))
            self._record_active_number(number)
            self._write_key_state({
                "number": number,
                "enteredBy": entered_by if entered_by in ENTERED_BY else "user",
                "since": time.time(),
                "createdSettings": created,
                "settingsPath": str(state.path),
            })
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
            underneath=self.label(underneath, long=False) if underneath else None,
            from_key=self.label(previous_key, long=False) if previous_key else None,
        )
        if emit_output:
            where = f"{self.label(source, long=False)} → " if source else "to "
            print(f"{accent('Switched')} {where}{self._landed_label(number, email)}")
            for line in followup:
                print(line)
        op = self._op_result(from_ref, account_ref(int(number), email), [], followup)
        op["how"] = display.HOW_HELPER
        return op

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

    def _leave_key(self) -> bool:
        """Remove qswap's helper line (any line of qswap's, valid or not),
        then put the run profiles' settings sharing back. Returns whether a
        line was removed. Raises :class:`SwitchError` when the settings file
        cannot be parsed, so the line cannot be found."""
        self._refuse_session_shell()
        state = self._key_state()
        with FileLock(self.lock_file):
            try:
                removed = claude_settings.remove(
                    remove_file_if_empty=bool(state.get("createdSettings")),
                )
            except claude_settings.SettingsError as e:
                path = claude_settings.shown_path(e.path or claude_settings.settings_path())
                raise SwitchError(
                    f"qswap cannot remove its key line: {path} {e.reason}. Fix the "
                    'file, or delete its "apiKeyHelper" line by hand, then retry.'
                ) from e
            self._write_key_state(None)
            self.restore_run_profiles()
        if removed:
            self._logger.info("Removed the key helper line from Claude Code's settings")
        return removed

    def reconcile_key_helper(self) -> list[str]:
        """Repair what a crash or a removed account left: a helper line that
        cannot give a key is removed, and run profiles holding a stale
        settings copy get their sharing back. Returns what was done, in
        words. Never raises."""
        done: list[str] = []
        try:
            state = self.key_helper_state()
            number, problem = self._helper_verdict(state)
            if number is not None and state.stale_command:
                # qswap moved (a reinstall): the line runs a command that is
                # gone. The same line, pointing at this qswap.
                claude_settings.install(number)
                done.append(
                    f"Updated the key line for #{number} to run qswap from its new place."
                )
            if problem is not None:
                self._leave_key()
                done.append(
                    f"Removed the key line for #{problem['number']} from Claude Code's settings."
                )
            stale = self.stale_profile_copies()
            if stale:
                restored = self.restore_run_profiles()
                if restored:
                    noun = "profile" if restored == 1 else "profiles"
                    done.append(f"Restored the shared settings in {restored} qswap run {noun}.")
        except Exception as e:
            self._logger.warning(f"Key helper repair failed: {e!r}")
        return done
