"""The ai-quota-swap Textual application.

Owns the snapshot poll loop (both providers, :class:`FleetSource`) and every
mutating action (switch, add, remove, disable), so the dashboard, the watch
page, the switch picker and the auto view all drive the same code paths. An
action goes to its account's own provider. Blocking switcher work always runs
in thread workers — the UI loop never touches file locks, keychain
subprocesses, or the network. A provider error is shown as the command line
shows it (``Error: …``); nothing ends in a traceback.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import replace
from functools import partial
from pathlib import Path

from textual.app import App
from textual.binding import Binding
from textual.reactive import reactive
from textual.worker import WorkerState

from ai_quota_swap import display, printer
from ai_quota_swap.settings import load_ui_settings, set_setting
from ai_quota_swap.snapshot_source import account_identity
from ai_quota_swap.tui.autoview import AutoScreen
from ai_quota_swap.tui.dashboard import DashboardScreen, WatchScreen
from ai_quota_swap.tui.data import (
    ActionResult,
    CostLine,
    Fleet,
    FleetSource,
    ProviderView,
    cost_line,
    format_duration,
    run_action,
    settings_root,
    usage_report,
)
from ai_quota_swap.tui.modals import (
    AddTokenModal,
    CommandModal,
    ConfirmModal,
    OutputModal,
    TokenForm,
)
from ai_quota_swap.tui.theme import QSWAP_DARK, QSWAP_LIGHT
from ai_quota_swap.tui.usageview import UsageScreen


class QswapApp(App):
    """ai-quota-swap interactive dashboard."""

    TITLE = "ai-quota-swap"
    CSS_PATH = str(Path(__file__).with_name("qswap.tcss"))
    # No command palette: actions live in the dashboard's nested menu, in
    # their own context — not in a global searchable list.
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+t", "toggle_theme", "Theme")]

    POLL_INTERVAL_S = 3.0  # matches the old watch view's recapture cadence
    # Snapshot age stays hidden while polling is healthy (age never exceeds
    # ~POLL_INTERVAL_S); past this it surfaces as a staleness alarm. 60s also
    # keeps format_duration in whole minutes, so the note never ticks per
    # second.
    SNAPSHOT_AGE_NOTE_S = 60.0
    # How often the cost line reads new session data.
    LEDGER_SCAN_S = 300.0

    fleet: reactive[Fleet | None] = reactive(None)
    refresh_status: reactive[str] = reactive("")
    busy: reactive[bool] = reactive(False)
    cost: reactive[CostLine | None] = reactive(None)

    def __init__(
        self,
        switcher,
        *,
        codex=None,
        start: str = "dashboard",
        detected: str | None = None,
        ledger: bool = True,
    ) -> None:
        super().__init__()
        self.switcher = switcher  # Claude Code's: settings and the ledger live in its root
        self.switchers = {p: s for p, s in (("claude", switcher), ("codex", codex)) if s is not None}
        self._start = start  # "dashboard" | "watch" (`qswap watch`)
        self._detected = detected  # terminal background sensed pre-driver, or None
        self.source = FleetSource(self.switchers)
        self._ledger = ledger
        self._ledger_lock = threading.Lock()
        self._store_only = False
        self._full_next = False
        self._normal_refreshing = False
        self._store_refreshing = False
        self._normal_started_at: float | None = None
        self._refresh_generation = 0
        self._applied_generation = 0
        self._last_refresh_error = ""
        # A session threshold from the auto view, per provider: the bars tick
        # at it while that view runs (never written to settings.json).
        self._session_thresholds: dict[str, float] = {}
        try:
            self._theme_name = load_ui_settings(switcher.backup_dir).theme
        except Exception:
            self._theme_name = "auto"

    # -- providers -------------------------------------------------------------

    def switcher_for(self, provider: str):
        return self.switchers.get(provider)

    def session_threshold(self, provider: str) -> float | None:
        return self._session_thresholds.get(provider)

    def set_session_threshold(self, provider: str, value: float | None) -> None:
        if value is None:
            self._session_thresholds.pop(provider, None)
        else:
            self._session_thresholds[provider] = value
        if self.fleet is not None:
            self.mutate_reactive(QswapApp.fleet)

    @property
    def snapshot(self):
        """Claude Code's :class:`AccountsSnapshot` from the last refresh."""
        view = self.fleet.view("claude") if self.fleet is not None else None
        return view.snapshot if view is not None else None

    def label(self, provider: str, number: str, *, long: bool = True) -> str:
        if self.fleet is None:
            return display.account_label(number)
        return self.fleet.label(provider, number, long=long)

    def on_mount(self) -> None:
        self.register_theme(QSWAP_DARK)
        self.register_theme(QSWAP_LIGHT)
        resolved = self._resolved_theme()
        # We own the theme; $TEXTUAL_THEME is intentionally not honoured.
        self.theme = f"qswap-{resolved}"
        printer.set_theme(resolved)
        self.push_screen(DashboardScreen())
        if self._start == "watch":
            # Stacked over the dashboard so Esc lands there, not on exit.
            self.push_screen(WatchScreen())
        self.set_interval(self.POLL_INTERVAL_S, self._tick)
        self.set_interval(1.0, self._update_refresh_status)
        self._tick()
        if self._ledger:
            self.refresh_cost(scan=False)
            self.refresh_cost(scan=True)
            self.set_interval(self.LEDGER_SCAN_S, partial(self.refresh_cost, scan=True))

    # -- snapshot poll loop ---------------------------------------------------

    def _tick(self) -> None:
        """Start one eligible refresh lane.

        Normal mode prefers the fetch-enabled lane. When that lane is blocked,
        the poll tick may still observe another process's store update through
        one store-only lane. Auto mode already has engines fetching, so it
        launches only store-only snapshots.
        """
        if self._store_only:
            self._start_store_refresh()
        elif not self._normal_refreshing:
            full, self._full_next = self._full_next, False
            self._start_normal_refresh(full=full)
        else:
            self._start_store_refresh()

    def _start_normal_refresh(self, *, full: bool) -> None:
        if self._normal_refreshing:
            return
        self._normal_refreshing = True
        self._normal_started_at = time.time()
        generation = self._next_refresh_generation()
        self._update_refresh_status()
        self.run_worker(
            partial(self._refresh_blocking, generation, "normal", full, False),
            thread=True,
            group="refresh-normal",
            exit_on_error=False,
            name="snapshot-refresh",
        )

    def _start_store_refresh(self) -> None:
        if self._store_refreshing:
            return
        self._store_refreshing = True
        generation = self._next_refresh_generation()
        self._update_refresh_status()
        self.run_worker(
            partial(self._refresh_blocking, generation, "store", False, True),
            thread=True,
            group="refresh-store",
            exit_on_error=False,
            name="snapshot-store-refresh",
        )

    def _next_refresh_generation(self) -> int:
        self._refresh_generation += 1
        return self._refresh_generation

    def _refresh_blocking(
        self, generation: int, lane: str, full: bool, store_only: bool
    ) -> None:
        fleet = self.source.take(full=full, store_only=store_only)
        self.call_from_thread(self._apply_snapshot, generation, lane, fleet)
        if self._store_only:
            self.source.refresh_plans()

    def _apply_snapshot(self, generation: int, lane: str, fleet: Fleet) -> None:
        if lane == "normal":
            self._normal_refreshing = False
            self._normal_started_at = None
        else:
            self._store_refreshing = False
        self._last_refresh_error = ""
        if generation >= self._applied_generation:
            self._applied_generation = generation
            self.fleet = fleet
        elif self.fleet is not None:
            # A later-started store repaint owns account metadata, but the
            # older worker may have completed a genuinely newer provider fetch.
            # SnapshotSource has already rejected per-account regressions, so
            # merge its canonical usage rows without restoring stale metadata.
            self.fleet = _merge_usage(self.fleet, fleet)
        self._update_refresh_status()

    def _update_refresh_status(self) -> None:
        parts: list[str] = []
        now = time.time()
        if self.fleet is not None:
            age = max(0.0, now - self.fleet.taken_at)
            if age >= self.SNAPSHOT_AGE_NOTE_S:
                parts.append(f"snapshot {format_duration(age)} ago")
        if self._normal_refreshing and self._normal_started_at is not None:
            elapsed = now - self._normal_started_at
            if elapsed >= self.POLL_INTERVAL_S:
                parts.append(f"refreshing {format_duration(elapsed)}")
        self.refresh_status = " · ".join(parts)

    def request_refresh(self, *, full: bool = False) -> None:
        if full:
            self._full_next = True
        self._tick()

    def set_store_only(self, value: bool) -> None:
        """Auto screen: the engines fetch, the poller only reads the stores."""
        self._store_only = value
        self.request_refresh()

    def on_worker_state_changed(self, event) -> None:
        if event.state is not WorkerState.ERROR:
            return
        group = event.worker.group
        if group in {"refresh-normal", "refresh-store"}:
            if group == "refresh-normal":
                self._normal_refreshing = False
                self._normal_started_at = None
            else:
                self._store_refreshing = False
            self._update_refresh_status()
            msg = str(event.worker.error)
            if msg != self._last_refresh_error:
                self._last_refresh_error = msg
                lane = "store refresh" if group == "refresh-store" else "refresh"
                self.notify(
                    f"{lane.capitalize()} failed: {msg}", severity="warning", timeout=6
                )
        elif group == "action":
            self.busy = False
            self.notify(f"Error: {event.worker.error}", title="Action failed", severity="error")
        elif group == "engine":
            self.notify(
                f"Auto-switch engine stopped: {event.worker.error}",
                severity="error",
            )
        elif group == "ledger":
            pass  # the cost line is a nicety; a ledger problem shows in `qswap usage`

    # -- the cost line ------------------------------------------------------------

    def ledger_registry(self) -> dict | None:
        get = getattr(self.switcher, "_get_sequence_data", None)
        if get is None:
            return None
        try:
            return get()
        except Exception:
            return None

    def read_ledger(self, *, days: int = 7, by: str = "day", scan: bool):
        """``qswap usage``'s report (blocking; from a worker). Scans one at a
        time: the cost line and the usage screen never read the same files
        twice at once."""
        root = settings_root(self.switcher)
        if not scan:
            return usage_report(root, days=days, by=by, scan=False, registry=self.ledger_registry())
        with self._ledger_lock:
            return usage_report(root, days=days, by=by, scan=True, registry=self.ledger_registry())

    def refresh_cost(self, *, scan: bool) -> None:
        if not self._ledger:
            return

        def work() -> None:
            line = cost_line(self.read_ledger(scan=scan))
            self.call_from_thread(self._set_cost, line, scan)

        self.run_worker(work, thread=True, group="ledger", exit_on_error=False,
                        name=f"ledger-{'scan' if scan else 'read'}")

    def _set_cost(self, line: CostLine | None, scanned: bool) -> None:
        if line is not None or scanned:
            self.cost = line

    # -- mutating actions (single-flight, captured, off-thread) ---------------

    def _start_action(self, label: str, fn, *, done=None) -> None:
        if self.busy:
            self.notify("Another action is still running", severity="warning")
            return
        self.busy = True
        self.run_worker(
            partial(self._action_blocking, label, fn, done),
            thread=True,
            group="action",
            exit_on_error=False,
            name=label,
        )

    def _action_blocking(self, label: str, fn, done) -> None:
        try:
            result = run_action(fn)
        except Exception as e:  # not a provider error: still a message, never a traceback
            result = ActionResult(False, f"Error: {e}\n", error=str(e))
        self.call_from_thread(self._action_done, label, result, done)

    def _action_done(self, label: str, result: ActionResult, done) -> None:
        self.busy = False
        self.request_refresh()
        if done is not None and done(result):
            return
        if not result.ok:
            self.push_screen(OutputModal(f"{label} failed", result.output))
            return
        if result.first_line:
            self.notify(result.first_line)

    # -- switching ------------------------------------------------------------------

    def do_switch(self, provider: str, number: str, *, anyway: bool = False) -> None:
        """Switch ``provider`` to account ``number``. A target that needs
        re-login is refused with the command line's message and an explicit
        "switch anyway" (``--anyway``); a stale or unreadable one switches
        with a warning."""
        sw = self.switcher_for(provider)
        if sw is None:
            return
        name = self.label(provider, str(number))

        def run() -> dict | None:
            notes: list[str] = []
            check = getattr(sw, "check_switch_target", None)
            if check is not None and str(number) != sw.current_account_number():
                notes = list(check(str(number), anyway=anyway) or [])
            payload = sw.switch_to(str(number), json_output=True)
            if isinstance(payload, dict) and notes:
                payload["warnings"] = [*notes, *payload.get("warnings", [])]
            return payload

        def done(result: ActionResult) -> bool:
            if not result.ok and not anyway and result.error and display.NEEDS_RELOGIN in result.error:
                self._offer_switch_anyway(provider, str(number), result.error)
                return True
            if not result.ok:
                return False
            self._report_switch(provider, result.payload or {})
            return True

        self._start_action(f"Switch to {name}", run, done=done)

    def _offer_switch_anyway(self, provider: str, number: str, error: str) -> None:
        # The command line's refusal, less its last line: the button below is
        # what "To switch to it anyway: qswap switch N --anyway" says to run.
        lines = [ln for ln in error.splitlines() if not ln.startswith("To switch to it anyway")]
        self.push_screen(
            ConfirmModal(
                "\n".join(lines),
                title="Needs re-login",
                yes_label="Switch anyway",
                default_no=True,
            ),
            lambda yes: self.do_switch(provider, number, anyway=True) if yes else None,
        )

    def _report_switch(self, provider: str, payload: dict) -> None:
        warnings = [str(w) for w in payload.get("warnings") or []]
        if payload.get("switched"):
            to = payload.get("to") or {}
            number = to.get("number")
            name = self.label(provider, str(number)) if number is not None else (to.get("email") or "")
            self.notify("\n".join([f"Switched to {name}", *warnings]),
                        severity="warning" if warnings else "information")
        else:
            message = str(payload.get("message") or payload.get("reason") or "No switch was made")
            self.notify("\n".join([message, *warnings]), severity="warning")

    def switch_best(self, provider: str) -> None:
        sw = self.switcher_for(provider)
        if sw is None:
            return

        def done(result: ActionResult) -> bool:
            if result.ok:
                self._report_switch(provider, result.payload or {})
                return True
            return False

        self._start_action(
            f"Switch {display.provider_name(provider)} to the best account",
            partial(sw.switch, strategy="best", json_output=True),
            done=done,
        )

    # -- disable, remove ---------------------------------------------------------------

    def do_toggle_disabled(self, provider: str, number: str) -> None:
        """Hold the account out of auto-rotation, or return it — reads its
        current state from the live snapshot to pick the direction."""
        found = self.fleet.find(provider, number) if self.fleet else None
        sw = self.switcher_for(provider)
        if found is None or sw is None:
            return
        _view, acc = found
        target = not acc.disabled
        name = self.label(provider, number)

        def done(result: ActionResult) -> bool:
            if not result.ok:
                return False
            verb = "Disabled" if target else "Enabled"
            tail = " It stays out of rotation; it can still be switched to." if target else ""
            self.notify(f"{verb} {name}.{tail}")
            return True

        self._start_action(
            f"{'Disable' if target else 'Enable'} {name}",
            partial(sw.set_account_disabled, number, target),
            done=done,
        )

    def confirm_remove(self, provider: str, number: str) -> None:
        name = self.label(provider, number)
        if provider == "codex":
            detail = ("Only qswap's saved copy of its login is deleted; the login itself "
                      "stays signed in. Do not run codex logout to clean up.")
        else:
            detail = "qswap deletes its saved copy of the login and its config backup."
        self.push_screen(
            ConfirmModal(
                f"Stop managing {name}?\n\n{detail}",
                title="Remove account",
                yes_label="Remove",
            ),
            partial(self._on_remove_confirm, provider, number),
        )

    def _on_remove_confirm(self, provider: str, number: str, confirmed: bool | None) -> None:
        sw = self.switcher_for(provider)
        if not confirmed or sw is None:
            return
        name = self.label(provider, number)

        def done(result: ActionResult) -> bool:
            if result.ok:
                self.notify(f"Removed {name}.")
                return True
            return False

        self._start_action(
            f"Remove {name}",
            partial(sw.remove_account, number, assume_yes=True),
            done=done,
        )

    # -- adding --------------------------------------------------------------------------

    def live_email(self, provider: str) -> str | None:
        """The provider's live login: the active managed account's email, or
        the unmanaged login's."""
        view = self.fleet.view(provider) if self.fleet else None
        if view is None:
            return None
        if view.active_number is not None:
            acc = view.account(view.active_number)
            return acc.email if acc else None
        return view.live_email

    def add_current(self, provider: str) -> None:
        sw = self.switcher_for(provider)
        if sw is None:
            return
        name = display.provider_name(provider)
        email = self.live_email(provider)
        who = f" ({email})" if email else ""
        self.push_screen(
            ConfirmModal(
                f"Save the current {name} login{who} as a managed account?\n\n"
                "If it is already managed, its saved login is replaced with this one.",
                title="Add account",
                yes_label="Add",
            ),
            partial(self._on_add_confirm, provider),
        )

    def _on_add_confirm(self, provider: str, confirmed: bool | None) -> None:
        sw = self.switcher_for(provider)
        if not confirmed or sw is None:
            return
        self._start_action(f"Add the current {display.provider_name(provider)} login", sw.add_account)

    def action_add_token(self) -> None:
        self.push_screen(AddTokenModal(), self._on_token_form)

    def _on_token_form(self, form: TokenForm | None) -> None:
        if form is None:
            return
        run = partial(
            self._start_action,
            "Add an account from a token",
            partial(
                self.switcher.add_account_from_token,
                token=form.token,
                email=form.email,
                slot=form.slot,
                assume_yes=True,
            ),
        )
        occupant = self._slot_occupant(form.slot)
        if occupant is not None:
            self.push_screen(
                ConfirmModal(
                    f"Slot {form.slot} holds {occupant}. Replace it?",
                    title="Replace an account",
                    yes_label="Replace",
                ),
                lambda confirmed: run() if confirmed else None,
            )
        else:
            run()

    def _slot_occupant(self, slot: int | None) -> str | None:
        if slot is None or self.fleet is None:
            return None
        view = self.fleet.view("claude")
        acc = view.account(str(slot)) if view else None
        return self.label("claude", str(slot)) if acc is not None else None

    def can_suspend(self) -> bool:
        driver = self._driver
        return bool(driver is not None and getattr(driver, "can_suspend", False)) and sys.platform != "win32"

    def codex_login(self) -> None:
        """Log another Codex account in, as ``qswap codex login`` does: the app
        steps aside for ``codex login`` in this terminal and comes back. Where
        it cannot step aside (a browser, Windows) it shows the command."""
        sw = self.switcher_for("codex")
        if sw is None:
            return
        if not self.can_suspend():
            self.push_screen(CommandModal(
                "Log in another Codex account",
                "Run this in a terminal. It logs the account in under a temporary Codex home, "
                "so the login you are on stays live; this dashboard shows the new account "
                "when it is saved.",
                "qswap codex login",
            ))
            return
        outcome: tuple[bool, str] = (False, "")
        with self.suspend():
            print()
            try:
                number = sw.login_new_account()
            except KeyboardInterrupt:
                outcome = (False, "codex login was interrupted; nothing was saved.")
            except Exception as e:  # ProviderError: the command line's words
                outcome = (False, f"Error: {e}")
            else:
                outcome = (True, f"Added Codex #{number}." if number else "Nothing was saved.")
        self.request_refresh()
        ok, message = outcome
        if ok:
            self.notify(message)
        else:
            self.push_screen(OutputModal("Log in another Codex account failed", message))

    # -- navigation -------------------------------------------------------------

    def action_refresh_full(self) -> None:
        self.request_refresh(full=True)
        self.notify("Refreshing usage…", timeout=2)

    def action_open_auto(self) -> None:
        if isinstance(self.screen, AutoScreen) or not (self.fleet and self.fleet.managed):
            return
        self.push_screen(AutoScreen())

    def action_open_watch(self) -> None:
        if isinstance(self.screen, WatchScreen) or not (self.fleet and self.fleet.managed):
            return
        self.push_screen(WatchScreen())

    def action_open_usage(self) -> None:
        if isinstance(self.screen, UsageScreen):
            return
        self.push_screen(UsageScreen())

    # -- theme --------------------------------------------------------------

    def _resolved_theme(self) -> str:
        """Concrete 'dark'/'light' for the current setting; auto → detected → dark."""
        if self._theme_name == "auto":
            return self._detected or "dark"
        return self._theme_name

    def apply_theme(self, name: str) -> None:
        """Switch the live theme: TUI + captured printer output + persistence.

        ``name`` is the setting; ``auto`` resolves against the pre-driver
        detection (never re-probes mid-session)."""
        self._theme_name = name
        resolved = self._resolved_theme()
        self.theme = f"qswap-{resolved}"
        printer.set_theme(resolved)
        try:
            set_setting(self.switcher.backup_dir, "ui.theme", name)
        except Exception as exc:  # persistence is best-effort; never crash the UI
            self.notify(f"Could not save theme: {exc}", severity="warning")

    def action_toggle_theme(self) -> None:
        order = ("dark", "light", "auto")
        nxt = order[(order.index(self._theme_name) + 1) % len(order)]
        self.apply_theme(nxt)
        self.notify(f"Theme: {nxt}")


def _merge_usage(current: Fleet, incoming: Fleet) -> Fleet:
    """``current`` with ``incoming``'s usage rows for the same accounts."""
    views = []
    for view in current.views:
        other = incoming.view(view.provider)
        if other is None:
            views.append(view)
            continue
        rows = {acc.number: acc for acc in other.accounts}
        accounts = tuple(
            replace(acc, usage=match.usage)
            if (match := rows.get(acc.number)) is not None
            and account_identity(acc) == account_identity(match)
            else acc
            for acc in view.accounts
        )
        snap = replace(
            view.snapshot, accounts=accounts,
            taken_at=max(view.snapshot.taken_at, other.snapshot.taken_at),
        )
        views.append(replace(view, snapshot=snap))
    return replace(current, views=tuple(views), taken_at=max(current.taken_at, incoming.taken_at))


__all__ = ["ProviderView", "QswapApp"]
