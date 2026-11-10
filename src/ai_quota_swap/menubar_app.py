"""The ``rumps`` glue: draw a :class:`~ai_quota_swap.menubar.Menu`, map each
:class:`~ai_quota_swap.menubar.Action` to a handler.

Every word and every layout decision is made by
:func:`ai_quota_swap.menubar.build_menu` and the notification texts in
``menubar_notify``; this module only puts them on screen, runs the reads in
background threads (``menubar_data``) and hosts the engines
(``menubar_engines``). Imported only by :func:`ai_quota_swap.menubar.run`,
after ``rumps`` imported, so it never loads in tests or off macOS.

Threads: the main thread runs AppKit and every handler; one worker thread
at a time reads both providers (:meth:`MenuBarApp.refresh_async`); one at a
time scans the ledger; each hosted engine has its own. Workers only rebind
plain attributes, which the one-second sync tick picks up on the main thread.
"""

from __future__ import annotations

import re
import threading
import time

from ai_quota_swap import launch_agent, manual_reset
from ai_quota_swap.exceptions import CredentialReadError, ProviderError, SwitchError
from ai_quota_swap.menubar import (
    ENGINE_HOST_LABEL,
    ICON,
    NOTIFY_TITLE,
    PROVIDERS,
    TOKEN_DIALOG_MESSAGE,
    TOKEN_DIALOG_TITLE,
    Action,
    Menu,
    MenuBarSettings,
    MenuInput,
    ProviderState,
    Row,
    _set_tooltip,
    _usage_log_key,
    build_menu,
    codex_login_help,
    codex_toggle_message,
    engine_state,
    error_text,
    format_usage_log,
    guarded,
    health_row,
    manual_switch_followup,
    parse_token_entry,
    relogin_confirmation,
    remove_dialog,
    resets_auto_confirmation,
    resets_setting,
    use_reset,
    wanted_engines,
)
from ai_quota_swap.menubar_data import (
    COST_FIRST_SCAN_DELAY_S,
    COST_SCAN_INTERVAL_S,
    CodexReader,
    Gathered,
    claude_state,
    heartbeats,
    ledger_stamp,
    read_cost,
    read_history,
    scan_ledger,
)
from ai_quota_swap.menubar_engines import ActiveChangeWatcher, EngineSupervisor, ResetRunner
from ai_quota_swap.menubar_notify import Notification, Notifier, manual_switch_text, ref_labeler

_ACCOUNT_REF = re.compile(r"Account-(\d+)(?: \([^)]*\))?")
#: Marks a finished background action in the event queue.
_ACTION_DONE = object()
#: Marks the result of a *Use a reset* claim in the event queue.
_RESET_DONE = object()


def run_app(switcher, rumps, AppKit) -> int:
    """Build and run the menu bar app over ``switcher`` (Claude Code's);
    Codex's switcher is built once Codex manages an account."""
    from ai_quota_swap.autoswitch import AutoSwitchEngine
    from ai_quota_swap.settings import (
        ResetSettings,
        load_codex_settings,
        load_engine_settings,
        load_reset_settings,
        load_settings,
        set_setting,
    )
    from ai_quota_swap.snapshot_source import SnapshotSource

    data_root = switcher.backup_dir
    settings_path = data_root / "menubar_settings.json"
    log_path = data_root / "ai-quota-swap.log"
    logger = switcher._logger

    class MenuBarApp(rumps.App):
        def __init__(self):
            super().__init__(ICON, quit_button=None)
            self.switcher = switcher
            self.settings = MenuBarSettings.load(settings_path)
            # The paced read path: per refresh it fetches only what the usage
            # store allows (the active account, at most one stale alternate),
            # so an open menu costs O(1) requests per window. Reused across
            # refreshes to hold its pacing state.
            self._claude_source = SnapshotSource(switcher)
            self._codex = CodexReader(data_root)
            self._data = Gathered(ProviderState("claude"), ProviderState("codex"))
            self._cost = None
            self._cost_stamp: tuple | None = None
            self._dirty = False
            self._refreshing = False
            self._scanning = False
            self._started = time.monotonic()
            self._first_scan_done = False
            self._thresholds: dict[str, float] = {}
            self._resets = ResetSettings()
            self._start_at_login = False
            self._last_usage_log: dict = {}
            self._events: list = []
            self._event_lock = threading.Lock()
            self._action_lock = threading.Lock()
            # Use a reset: manual_reset.spend (the `qswap reset N --grant ID
            # --yes` sequence) in a worker; the result comes back as an event.
            self._reset_runner = ResetRunner(
                manual_reset.spend, lambda note: self._queue_event(_RESET_DONE, note),
                lock=self._action_lock,
            )
            self._notifier = Notifier()
            self._supervisor = EngineSupervisor(self._make_engine, self._queue_event)
            self._watcher = ActiveChangeWatcher(
                paths={"claude": self.switcher._get_claude_config_path, "codex": _codex_auth_path},
                read_active={"claude": self.switcher.live_login_email, "codex": _codex_live_email},
            )
            self._health_item = None
            self._health_row: Row | None = None
            self._read_disk_settings()
            # Claude Code's engine starts at launch, as it always has; Codex's
            # waits for the first read to show that Codex manages an account.
            self._apply_engines()
            self.rebuild_menu()
            # Background display refresh on the user's interval, plus a fast
            # UI-sync tick that applies reads and engine events on the main
            # thread, and the ledger scan.
            self.refresh_timer = rumps.Timer(self.on_refresh_tick, self.settings.refresh_interval)
            self.refresh_timer.start()
            self.sync_timer = rumps.Timer(self.on_sync_tick, 1)
            self.sync_timer.start()
            # rumps fires a timer as soon as it starts, so the ledger scan's
            # timer starts with the first scan, a minute after launch.
            self.cost_timer = rumps.Timer(self.on_cost_tick, COST_SCAN_INTERVAL_S)
            self.refresh_async()

        # ---- reads ----------------------------------------------------------
        def refresh_async(self, full=False):
            if self._refreshing:
                return  # one worker at a time (the sources' pacing state)
            self._refreshing = True
            threading.Thread(target=self._worker, args=(full,), daemon=True).start()

        def _worker(self, full):
            try:
                now = time.time()
                previous = self._data
                claude_store_only = self._supervisor.running("claude") and not full
                codex_store_only = self._supervisor.running("codex") and not full
                try:
                    claude = claude_state(
                        self.switcher, self._claude_source,
                        full=full, store_only=claude_store_only, now=now,
                    )
                except Exception:
                    # Keep the last good read rather than blanking the menu.
                    logger.debug("menu bar Claude Code read failed", exc_info=True)
                    claude = previous.claude
                try:
                    codex = self._codex.state(full=full, store_only=codex_store_only, now=now)
                except Exception:
                    logger.debug("menu bar Codex read failed", exc_info=True)
                    codex = previous.codex
                stamp = ledger_stamp(data_root)
                if stamp != self._cost_stamp:
                    self._cost = read_cost(data_root)
                    self._cost_stamp = stamp
                self._data = Gathered(
                    claude, codex, read_history(log_path), heartbeats(data_root), now,
                )
                self._log_usage(self._data)
                self._dirty = True  # picked up by on_sync_tick on the main thread
                if claude_store_only:
                    self._claude_source.refresh_plans()
            except Exception:
                logger.debug("menu bar read failed", exc_info=True)
            finally:
                self._refreshing = False

        def _log_usage(self, data: Gathered):
            """Log each account's 5h and 7d windows when they change (de-duped
            per account on the percentages, so an idle machine doesn't churn
            the rotating log with identical lines)."""
            both = data.codex.count > 0
            for acc in data.claude.accounts + data.codex.accounts:
                last_good = acc.usage()
                key = _usage_log_key(last_good)
                ident = (acc.provider, acc.number)
                if key == (None, None) or self._last_usage_log.get(ident) == key:
                    continue
                line = format_usage_log(acc.label(provider_prefix=both, long=False), last_good)
                if line:
                    logger.info(line)
                    self._last_usage_log[ident] = key

        def _scan_async(self):
            if self._scanning:
                return
            self._scanning = True
            threading.Thread(target=self._scan, daemon=True).start()

        def _scan(self):
            try:
                registry = None
                try:
                    registry = self.switcher._get_sequence_data()
                except Exception:
                    pass
                scan_ledger(data_root, registry=registry)
                self._cost = read_cost(data_root)
                self._cost_stamp = ledger_stamp(data_root)
                self._dirty = True
            except Exception:
                logger.debug("menu bar ledger scan failed", exc_info=True)
            finally:
                self._scanning = False

        # ---- ticks ---------------------------------------------------------
        def on_refresh_tick(self, _timer):
            self.refresh_async()

        def on_cost_tick(self, _timer):
            self._scan_async()

        def on_sync_tick(self, _timer):
            if self._dirty:
                self._dirty = False
                self._apply_engines()
                self.rebuild_menu()
            self._detect_active_change()
            self._drain_events()
            self._refresh_health_row()
            if not self._first_scan_done and time.monotonic() - self._started >= COST_FIRST_SCAN_DELAY_S:
                self._first_scan_done = True
                self.cost_timer.start()  # scans now, then every COST_SCAN_INTERVAL_S

        def _detect_active_change(self):
            # Reflect switches from any source (menu, CLI, an engine, a /login)
            # within about a second: a stat per provider, a read only when
            # the file changed, a refresh only when the login differs.
            if self._refreshing:
                return  # a worker is in flight; it refreshes the marks
            shown = {}
            for provider in PROVIDERS:
                active = self._state(provider).active
                shown[provider] = active.email if active is not None else None
            if self._watcher.poll(shown):
                self.refresh_async()

        # ---- engines --------------------------------------------------------
        def _both(self) -> bool:
            return self._data.codex.count > 0

        def _switcher_for(self, provider):
            return self._codex.ensure() if provider == "codex" else self.switcher

        def _make_engine(self, provider, emit):
            sw = self._switcher_for(provider)
            if provider == "codex":
                sw.require_accounts()  # one clear line instead of an idle engine
            engine = AutoSwitchEngine(sw, load_engine_settings(data_root, provider), emit, dry_run=False)
            engine.host_label = ENGINE_HOST_LABEL
            return engine

        def _apply_engines(self):
            wanted = wanted_engines(self.settings, codex_accounts=self._data.codex.count)
            for provider, reason in self._supervisor.apply(wanted).items():
                self._notify(self._notifier.engine_failed(provider, reason, both=self._both()))

        def _queue_event(self, provider, event):
            # Runs on an engine thread; must not raise. The main thread
            # surfaces notifications and reacts on the sync tick.
            with self._event_lock:
                self._events.append((provider, event))

        def _drain_events(self):
            with self._event_lock:
                events, self._events = self._events, []
            both = self._both()
            for provider, ev in events:
                if provider is _ACTION_DONE:
                    self._finish_action(*ev)
                    continue
                if provider is _RESET_DONE:
                    self._notify(ev)
                    self.rebuild_menu()  # the account's rows are usable again
                    self.refresh_async()  # and show the refilled window
                    continue
                kind = getattr(ev, "kind", "")
                if kind == "engine-stopped":
                    if self._supervisor.stopped(provider, ev):
                        logger.warning(
                            "%s auto-switch stopped (%s); the menu bar keeps showing usage "
                            "but will not switch until it is restarted", provider, ev.human(),
                        )
                        self._notify(self._notifier.engine_stopped(provider, ev.human(), both=both))
                        self.rebuild_menu()
                        self.refresh_async()  # display fetching resumes on its own
                    continue
                note = self._notifier.event(provider, ev, both=both, now=time.monotonic())
                if note is not None:
                    if kind == "error":
                        logger.warning("%s auto-switch %s", provider, ev.human())
                    self._notify(note)
                dry = getattr(ev, "dry_run", False)
                if (kind == "switch" and not dry) or (kind == "limit-reset" and getattr(ev, "used", False)):
                    self.refresh_async()  # show the switch or the refilled window promptly

        # ---- drawing --------------------------------------------------------
        def _read_disk_settings(self):
            """Values the menu shows that live in other files; read on rebuild,
            not on every tick."""
            thresholds = {}
            try:
                thresholds["claude"] = float(load_settings(data_root).threshold)
            except Exception:
                pass
            try:
                thresholds["codex"] = float(load_codex_settings(data_root).threshold)
            except Exception:
                pass
            self._thresholds = thresholds
            try:
                # resets.* as `qswap config set` left them: a change made in a
                # terminal shows at the next rebuild.
                self._resets = load_reset_settings(data_root)
            except Exception:
                self._resets = ResetSettings()
            try:
                self._start_at_login = bool(launch_agent.start_at_login_enabled())
            except Exception:
                self._start_at_login = False

        def _menu_input(self) -> MenuInput:
            now = time.time()
            data = self._data
            wanted = wanted_engines(self.settings, codex_accounts=data.codex.count)
            engines = {
                p: engine_state(
                    p, enabled=wanted[p], running=self._supervisor.running(p),
                    failure=self._supervisor.failure(p), heartbeat=data.heartbeats.get(p), now=now,
                )
                for p in PROVIDERS
            }
            return MenuInput(
                claude=data.claude, codex=data.codex, settings=self.settings, now=now,
                engines=engines, cost=self._cost, history=data.history,
                start_at_login=self._start_at_login, thresholds=self._thresholds,
                can_add_token=hasattr(self.switcher, "add_account_from_token"),
                resets=self._resets, resets_busy=self._reset_runner.busy(),
            )

        def rebuild_menu(self):
            self._read_disk_settings()
            menu = build_menu(self._menu_input())
            self.title = menu.title
            self._draw(menu)

        def _draw(self, menu: Menu):
            # Stop a rumps memory leak: rumps registers each item's callback in
            # the process-global NSApp._ns_to_py_and_callback, but Menu.clear()
            # never removes them, so rebuilding on every refresh leaks every
            # item forever (~1GB after days). Walk the native NSMenu tree
            # (the rumps dict is keyed by title and drops same-title items).
            # Guard the private attribute: if rumps renames it, degrade to
            # "leaks again" rather than crash on every rebuild.
            reg = getattr(rumps.rumps.NSApp, "_ns_to_py_and_callback", None)
            if reg is not None:
                def purge(nsmenu):
                    for it in nsmenu.itemArray():
                        reg.pop(it, None)
                        sub = it.submenu()
                        if sub is not None:
                            purge(sub)
                purge(self.menu._menu)
            self.menu.clear()
            self._health_item = None
            for i, row in enumerate(menu.rows()):
                if row.separator:
                    self.menu.add(None)
                    continue
                item = self._item(row)
                # A unique key, not the title: rumps keys items by title and
                # silently drops a second row with the same text.
                self.menu[f"row-{i:03d}"] = item
                if i == 0:
                    self._health_item = item
                    self._health_row = row

        def _item(self, row: Row):
            if row.children:
                item = rumps.MenuItem(row.text)
                for j, child in enumerate(row.children):
                    if child.separator:
                        item.add(None)
                    else:
                        item[f"child-{j:03d}"] = self._item(child)
            else:
                callback = None
                if row.action is not None:
                    action = row.action
                    callback = lambda _sender, a=action: self.dispatch(a)  # noqa: E731
                item = rumps.MenuItem(row.text, callback=callback)
            item.state = 1 if row.checked else 0
            if row.tooltip:
                _set_tooltip(item, row.tooltip)
            if row.attention:
                _mark_attention(AppKit, item, row.text)
            return item

        def _refresh_health_row(self):
            """Keep the first row current between rebuilds. Rebuilds follow
            successful reads; a read that keeps failing never triggers one,
            so the row would say "read 2m ago" for hours, which is the very
            state it exists to show. A change of state (attention on or off)
            rebuilds, since the row's action and the title change with it."""
            if self._health_item is None or self._health_row is None:
                return
            row = health_row(self._menu_input())
            if row.attention != self._health_row.attention:
                self.rebuild_menu()
            elif row.text != self._health_row.text:
                self._health_item.title = row.text
                if row.attention:
                    _mark_attention(AppKit, self._health_item, row.text)
                self._health_row = row

        # ---- feedback -------------------------------------------------------
        def _notify(self, note: Notification | None):
            # rumps raises when the process has no bundle identifier; a lost
            # notification must not end the launch or drop the rest of a batch.
            if note is None:
                return
            try:
                rumps.notification(note.title, note.subtitle, note.body)
            except Exception:
                logger.warning("could not show a notification: %s", note.body, exc_info=True)

        def _activate(self):
            # A menu bar (accessory) app is not the active app, so a modal
            # dialog can render blank or behind other windows until the app
            # comes forward.
            try:
                AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            except Exception:
                pass

        def _alert(self, message: str):
            self._activate()
            rumps.alert(title=NOTIFY_TITLE, message=message)

        def _confirm(self, title: str, message: str, ok: str) -> bool:
            self._activate()
            return rumps.alert(title=title, message=message, ok=ok, cancel="Cancel") == 1

        def _confirm_cancel_default(self, title: str, message: str, ok: str) -> bool:
            """A confirmation whose default (Return) button is Cancel, for
            what cannot be undone."""
            self._activate()
            return _ask_cancel_default(AppKit, title, message, ok)

        def dispatch(self, action: Action):
            """Run a row's handler. No error ever escapes to rumps: a provider
            error shows the command line's words, anything else its type."""
            handler = getattr(self, "_do_" + action.kind.replace("-", "_"), None)
            if handler is None:
                return
            guarded(
                lambda: handler(action), alert=self._alert,
                log=lambda e: logger.error(
                    "menu bar action %s failed", action.kind, exc_info=(type(e), e, e.__traceback__),
                ),
            )

        # ---- handlers: accounts ---------------------------------------------
        def _state(self, provider) -> ProviderState:
            return self._data.codex if provider == "codex" else self._data.claude

        def _labeler(self, provider):
            return ref_labeler({
                a.number: {"alias": a.alias, "email": a.email} for a in self._state(provider).accounts
            })

        def _relabel(self, provider, text: str) -> str:
            label = self._labeler(provider)
            return _ACCOUNT_REF.sub(lambda m: label({"number": m.group(1)}), text)

        def _after_switch(self, provider, result, notes=()):
            if isinstance(result, dict) and not result.get("switched", True):
                message = result.get("message")
                if message:
                    self._notify(Notification(self._relabel(provider, str(message))))
                return
            backend = getattr(self.switcher, "_last_active_credentials_backend", None)
            text = manual_switch_text(
                provider, result, both=self._both(), label=self._labeler(provider),
                followup=manual_switch_followup(provider, result, backend=backend),
                warnings=tuple(notes),
            )
            if text:
                self._notify(Notification(text))
            self.refresh_async()

        def _in_background(self, work, done):
            """Run a slow action off the main thread: a switch reads the
            Keychain, may read usage over the network (the pre-switch check)
            and, for Codex, restarts its background server, which can take
            seconds. ``done(result)`` then runs on the main thread (the sync
            tick); an error becomes an alert. One action at a time."""
            def run():
                with self._action_lock:
                    try:
                        result, error = work(), None
                    except Exception as e:
                        result, error = None, e
                with self._event_lock:
                    self._events.append((_ACTION_DONE, (done, result, error)))

            threading.Thread(target=run, daemon=True, name="menubar-action").start()

        def _finish_action(self, done, result, error):
            if error is not None:
                if not isinstance(error, ProviderError):
                    logger.error(
                        "menu bar action failed", exc_info=(type(error), error, error.__traceback__),
                    )
                self._alert(error_text(error))
                return
            guarded(lambda: done(result), alert=self._alert)

        def _do_switch(self, a: Action, anyway: bool = False):
            sw = self._switcher_for(a.provider)
            acc = next((x for x in self._state(a.provider).accounts if x.number == a.number), None)
            check = acc is None or not acc.active

            def work():
                notes: list[str] = []
                if check:
                    try:
                        notes = list(sw.check_switch_target(a.number, anyway=anyway) or [])
                    except SwitchError as e:
                        if anyway:
                            raise
                        return ("refused", str(e), [])
                return ("done", sw.switch_to(str(a.number), json_output=True), notes)

            def done(outcome):
                kind, value, notes = outcome
                if kind == "refused":
                    # Needs re-login: the command line refuses without
                    # --anyway; here the same explanation asks first.
                    if self._confirm("Switch anyway?", relogin_confirmation(value), "Switch anyway"):
                        self._do_switch(a, anyway=True)
                    return
                self._after_switch(a.provider, value, notes)

            self._in_background(work, done)

        def _do_rotate(self, a: Action):
            self._in_background(
                lambda: self.switcher.switch(strategy=a.value, json_output=True),
                lambda result: self._after_switch("claude", result),
            )

        def _do_add_current(self, a: Action):
            self._switcher_for(a.provider).add_account()
            self.refresh_async()

        def _do_add_token(self, _a: Action):
            entry = _token_dialog(rumps, AppKit, self._activate)
            if entry is None:
                return
            try:
                token, email = parse_token_entry(*entry)
            except ValueError as e:
                self._alert(str(e))
                return
            self.switcher.add_account_from_token(token=token, email=email, slot=None)
            self.refresh_async()

        def _do_codex_login_help(self, _a: Action):
            self._activate()
            if rumps.alert(
                title="Log in another Codex account", message=codex_login_help(),
                ok="Copy command", cancel="Close",
            ) == 1:
                _copy_to_clipboard(AppKit, "qswap codex login")

        def _do_remove(self, a: Action):
            acc = next((x for x in self._state(a.provider).accounts if x.number == a.number), None)
            label = acc.label() if acc is not None else f"#{a.number}"
            title, message = remove_dialog(a.provider, label)
            if not self._confirm(title, message, "Remove"):
                return
            self._switcher_for(a.provider).remove_account(str(a.number), assume_yes=True)
            self.refresh_async()

        def _do_toggle_disabled(self, a: Action):
            self._switcher_for(a.provider).set_account_disabled(str(a.number), bool(a.value))
            self.refresh_async()

        def _do_refresh_creds(self, _a: Action):
            if self.switcher._get_current_account() is None:
                self._alert("Claude Code is not logged in. Log in with claude (/login), then try again.")
                return
            try:
                self.switcher.add_account(slot=None)
            except CredentialReadError:
                # The Keychain did not answer (locked, or `security` timed
                # out). A launchd agent reads the Keychain like any other
                # process of the session, so relaunching is not a fix.
                self._alert(
                    "Couldn't read the active credential: the Keychain didn't answer "
                    "(locked, or the security call timed out). Unlock it or try again "
                    "in a moment; qswap add in a terminal shows the full error."
                )
                return
            self.refresh_async()

        def _do_open_log(self, _a: Action):
            import subprocess

            # Reveal the log in Finder (-R); if it doesn't exist yet, open the dir.
            target = log_path if log_path.exists() else log_path.parent
            subprocess.run(["open", "-R", str(target)], check=False)

        def _do_refresh_now(self, _a: Action):
            self.refresh_async(full=True)  # explicit refresh: a store-governed pass

        def _do_quit(self, _a: Action):
            self._supervisor.stop_all()
            rumps.quit_application()

        # ---- handlers: engines and settings --------------------------------
        def _do_restart_engines(self, _a: Action):
            for provider, reason in self._supervisor.retry_failed().items():
                if reason:
                    self._notify(self._notifier.engine_failed(provider, reason, both=self._both()))
            self.rebuild_menu()

        def _do_toggle_auto(self, a: Action):
            provider = a.provider or "claude"
            enable = not self.settings.auto_enabled(provider)
            if enable and provider == "codex":
                try:
                    codex = load_codex_settings(data_root)
                    message = codex_toggle_message(codex.threshold, restart_daemon=codex.restart_daemon)
                except Exception:
                    message = codex_toggle_message(None)
                if not self._confirm("Auto-switch Codex", message, "Turn on"):
                    return
            self.settings.set_auto_enabled(provider, enable)
            self.settings.save(settings_path)
            self._apply_engines()
            self.rebuild_menu()

        def _do_threshold(self, a: Action):
            key = "codex.threshold" if a.provider == "codex" else "autoswitch.threshold"
            try:
                set_setting(data_root, key, str(a.value))
            except Exception as e:
                self._alert(f"Couldn't set the threshold: {error_text(e) if isinstance(e, ProviderError) else e}")
                return
            failure = self._supervisor.restart(a.provider or "claude")  # apply now if running
            if failure:
                self._notify(self._notifier.engine_failed(a.provider or "claude", failure, both=self._both()))
            self.rebuild_menu()

        def _do_resets_auto(self, a: Action):
            # Off → on can spend something irreversible: ask once.
            question = resets_auto_confirmation(self._resets.auto, str(a.value))
            if question and not self._confirm("Use resets automatically", question, "Turn on"):
                return
            self._write_reset_setting(a)

        def _do_toggle_weekly_for_model(self, a: Action):
            self._write_reset_setting(a)

        def _write_reset_setting(self, a: Action):
            """The key `qswap config set` writes, through the same writer.
            Running engines read resets.* from settings.json on every tick
            (switcher.reset_settings), so nothing is restarted."""
            key, value = resets_setting(a, self._resets)
            try:
                set_setting(data_root, key, value)
            except Exception as e:
                self._alert(f"Couldn't change {key}: {error_text(e) if isinstance(e, ProviderError) else e}")
                return
            self.rebuild_menu()

        def _do_use_reset(self, a: Action):
            started = use_reset(
                self._menu_input(), a,
                confirm=lambda title, message: self._confirm_cancel_default(title, message, "Use reset"),
                run=lambda provider, number, grant_id, **kw: self._reset_runner.run(
                    self._switcher_for(provider), provider, number, grant_id, **kw,
                ),
            )
            if started:
                self.rebuild_menu()  # its rows read "in progress"

        def _save_and_rebuild(self):
            self.settings.save(settings_path)
            self.rebuild_menu()

        def _do_toggle_name(self, _a: Action):
            self.settings.show_account_name = not self.settings.show_account_name
            self._save_and_rebuild()

        def _do_toggle_scoped(self, _a: Action):
            self.settings.title_scoped = not self.settings.title_scoped
            self._save_and_rebuild()

        def _do_title_pct(self, a: Action):
            self.settings.title_pct = str(a.value)
            self._save_and_rebuild()

        def _do_title_follows(self, a: Action):
            self.settings.title_follows = str(a.value)
            self._save_and_rebuild()

        def _do_interval(self, a: Action):
            secs = int(a.value)
            self.settings.refresh_interval = secs
            # rumps 0.4.0's Timer.interval setter is a no-op while running
            # unless a full interval has elapsed; stop/start forces the new
            # cadence to take effect immediately.
            self.refresh_timer.stop()
            self.refresh_timer.interval = secs
            self.refresh_timer.start()
            self._save_and_rebuild()

        def _do_toggle_login(self, _a: Action):
            # Moves the plist between its two homes; launchd is not touched,
            # so the app whose menu was just clicked keeps running either way.
            enabled = not launch_agent.start_at_login_enabled()
            try:
                launch_agent.set_start_at_login(enabled, backup_dir=data_root)
            except (ProviderError, OSError) as e:
                self._alert(f"Couldn't change Start at login: {e}")
                return
            self.rebuild_menu()

    MenuBarApp().run()
    return 0


def _ask_cancel_default(AppKit, title: str, message: str, ok: str) -> bool:
    """An alert with Cancel as the default button and ``ok`` beside it;
    True only when ``ok`` was clicked."""
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_("Cancel")  # the first button is the default
    alert.addButtonWithTitle_(ok)
    return alert.runModal() == AppKit.NSAlertSecondButtonReturn


def _codex_auth_path():
    from ai_quota_swap.codex import auth as codex_auth

    return codex_auth.auth_path(codex_auth.codex_home())


def _codex_live_email():
    from ai_quota_swap import codex_cli

    return codex_cli.live_codex_email()


def _mark_attention(AppKit, item, text: str) -> None:
    """Draw a row that needs the user in the system orange, in the menu's own
    font (an attributed title without a font falls back to Helvetica). The
    words already say it; colour only helps the eye, so any failure leaves
    the plain row."""
    try:
        attrs = {
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.systemOrangeColor(),
            AppKit.NSFontAttributeName: AppKit.NSFont.menuFontOfSize_(0),
        }
        title = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        item._menuitem.setAttributedTitle_(title)
    except Exception:
        pass


def _copy_to_clipboard(AppKit, text: str) -> None:
    try:
        board = AppKit.NSPasteboard.generalPasteboard()
        board.clearContents()
        board.setString_forType_(text, AppKit.NSPasteboardTypeString)
    except Exception:
        pass


def _token_dialog(rumps, AppKit, activate) -> tuple[str, str] | None:
    """One dialog, token first (hidden as it is typed), email optional.
    ``(token, email)``, or None when cancelled. Falls back to a one-field
    rumps window (token, then the email after a space) if the two-field
    alert cannot be built."""
    activate()
    try:
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(TOKEN_DIALOG_TITLE)
        alert.setInformativeText_(TOKEN_DIALOG_MESSAGE)
        alert.addButtonWithTitle_("Add")
        alert.addButtonWithTitle_("Cancel")
        view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 320, 56))
        # rumps' Editing fields handle Cmd+X/C/V/A/Z themselves: an accessory
        # app has no Edit menu, so plain text fields would not paste.
        secure = getattr(rumps.rumps, "SecureEditing", AppKit.NSSecureTextField)
        plain = getattr(rumps.rumps, "Editing", AppKit.NSTextField)
        token = secure.alloc().initWithFrame_(AppKit.NSMakeRect(0, 32, 320, 24))
        token.setPlaceholderString_("token (required)")
        email = plain.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 320, 24))
        email.setPlaceholderString_("email (optional)")
        view.addSubview_(token)
        view.addSubview_(email)
        alert.setAccessoryView_(view)
        alert.window().setInitialFirstResponder_(token)
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return None
        return str(token.stringValue()), str(email.stringValue())
    except Exception:
        window = rumps.Window(
            title=TOKEN_DIALOG_TITLE,
            message=TOKEN_DIALOG_MESSAGE + " To add an email, type it after the token and a space.",
            ok="Add", cancel="Cancel", dimensions=(360, 24),
        )
        response = window.run()
        if response.clicked != 1:
            return None
        return str(response.text), ""
