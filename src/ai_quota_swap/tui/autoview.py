"""Live auto-switch screen: the real engines, visualized.

One :class:`AutoSwitchEngine` per provider with accounts, each in a thread
of its own, hosted as the menu bar hosts its engines
(``menubar_engines.EngineSupervisor``). Opens in **dry-run** — opening a view must never start switching
accounts on its own; going live is an explicit, confirmed action, per
provider. The engine's own state file semantics (shared cooldown, quarantine
list, state lock) make it safe to run alongside an external ``qswap auto``.

With Claude Code accounts only, the screen is one panel: the active account's
card, the engine badge and its line, the "Next best" list, and the log. With
Codex accounts too, each provider gets a panel of its own with its own badge,
threshold and toggle: Tab moves between them, or both stand side by side on a
terminal at least 140 columns wide. The log is shared and tags each line with
its provider. Its lines are ``qswap auto``'s (:class:`autolog.AutoLog`).
While the screen is up, the app's snapshot poller reads the stores only: the
engines are the only fetchers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static

from ai_quota_swap import api_spend, display, oauth, printer
from ai_quota_swap.autolog import AutoLog
from ai_quota_swap.autoswitch import (
    AutoSwitchEngine,
    AutoSwitchEvent,
    pct_label,
    key_entry_hold,
    rotation_candidates,
)
from ai_quota_swap.json_output import USAGE_RELOGIN_REQUIRED
from ai_quota_swap.labels import PROVIDER_SHORT
from ai_quota_swap.lines import binding_window
from ai_quota_swap.menubar_engines import EngineStoppedEvent, EngineSupervisor
from ai_quota_swap.settings import SETTING_SPECS, load_engine_settings, parse_model_names
from ai_quota_swap.tui import data
from ai_quota_swap.tui.modals import ConfirmModal
from ai_quota_swap.tui.theme import Palette, from_printer
from ai_quota_swap.tui.widgets import AccountsPanel, header_text, level_colour

if TYPE_CHECKING:
    from ai_quota_swap.tui.app import QswapApp

#: Two panels stand side by side from this width.
WIDE = 140
#: The provider tag in front of a log line when both providers run.
TAG_WIDTH = 7


def event_text(event: AutoSwitchEvent, *, palette: Palette = Palette.DARK) -> Text:
    """One event as a single log line (``event.human()``), styled by kind."""
    roles = {"switch": "accent", "limit-reset": "accent", "error": "sev_warn",
             "account-quarantined": "sev_warn", "all-exhausted": "sev_crit"}
    quiet = {"poll", "no-switch", "sleep", "account-unquarantined"}
    role = roles.get(event.kind)
    style = getattr(palette, role) if role else (palette.muted if event.kind in quiet else palette.foreground)
    text = Text()
    text.append(f"{data.clock_stamp()}  ", style=palette.muted)
    text.append(event.human(), style=style)
    return text


@dataclass(frozen=True)
class Candidate:
    """One row of "Next best"."""

    number: str
    label: str
    words: str
    level: str = "dim"  # "ok" | "warn" | "limit" | "dim"
    line: str = ""


def next_best(
    view: data.ProviderView,
    settings,
    *,
    threshold: float | None = None,
) -> list[Candidate]:
    """The accounts the engine would consider as targets, best first.

    The set is the engine's own rule (``autoswitch.rotation_candidates``):
    not the active account, not disabled, not set aside, not an API-key
    account unless ``autoswitch.includeApiKeyAccounts``, not one at its
    budget. An account whose saved login was refused is set aside the first
    time the engine tries it, so it is left out too. The order is ``best``'s:
    most room under each account's own lines, measured on the global line;
    accounts with no reading the engine can use follow, then API-key
    accounts, which it lands on only as a last resort.
    """
    models = parse_model_names(settings.model) if settings else ()
    line_global = threshold if threshold is not None else (settings.threshold if settings else 90.0)
    weekly = settings.weekly_threshold if settings else None
    include_api = bool(getattr(settings, "include_api_key_accounts", False))
    accounts = {a.number: a for a in view.accounts}
    set_aside = set(view.set_aside) | {
        a.number for a in view.accounts if a.usage.sentinel == USAGE_RELOGIN_REQUIRED
    }
    picked = rotation_candidates(
        [a.number for a in view.accounts if a.switchable and not a.disabled],
        view.active_number,
        set_aside=set_aside,
        kind_of=lambda n: accounts[n].kind,
        include_api_key_accounts=include_api,
        entries={n: a.usage for n, a in accounts.items()},
    )
    ranked: list[tuple[float, int, Candidate]] = []
    rest: list[Candidate] = []
    for order, number in enumerate(picked.oauth):
        acc = accounts[number]
        name = display.account_label(acc.number, alias=acc.alias or None, email=acc.email)
        value = acc.usage.decision_value()
        own = acc.policy.threshold if acc.policy.threshold is not None else line_global
        binding = (
            binding_window(oauth.relevant_windows(value, models), own, weekly, scale=line_global)
            if isinstance(value, dict) else None
        )
        if binding is None:
            block = data.block_for(view, acc)
            from ai_quota_swap.tui.widgets import summary_parts

            words = "".join(t for t, _r in summary_parts(block))
            if block.state == "ok":
                words = "usage unreadable this check"
            rest.append(Candidate(number, name, words))
            continue
        level = display.severity(binding.pct, binding.line)
        show_line = weekly is not None or acc.policy.threshold is not None
        cand = Candidate(
            number, name, f"{binding.label} {binding.pct:3.0f}% used", level,
            f"(line {pct_label(binding.line)}%)" if show_line else "",
        )
        if binding.pct >= 100:
            rest.insert(0, replace(cand, line=(cand.line + " · limit reached").strip(" ·")))
        else:
            ranked.append((-binding.line_headroom, order, cand))
    out = [c for _k, _o, c in sorted(ranked, key=lambda t: (t[0], t[1]))] + rest
    # A key the engine would not enter says why, after those it would
    # (decisions A2 and A4; ux.md: a skipped account shows why everywhere).
    unused: list[Candidate] = []
    now = time.time()
    for number in picked.api_key:
        acc = accounts[number]
        name = display.account_label(acc.number, alias=acc.alias or None, email=acc.email)
        hold = key_entry_hold(getattr(acc.usage, "api_spend", None), now=now)
        if hold == "key-no-limit":
            unused.append(Candidate(number, name, "API key · no budget or credit, not used"))
        elif hold == "spend-unknown":
            unused.append(Candidate(number, name, f"API key · {api_spend.LIMIT_NOT_ENFORCED}"))
        elif hold == "limit-latched":
            latched = api_spend.latch_of(getattr(acc.usage, "api_spend", None)) or "budget"
            words = api_spend.LATCHED_WORDS[latched]
            unused.append(Candidate(number, name, f"API key · {words}, not used"))
        else:
            out.append(Candidate(number, name, "API key · only as a last resort"))
    return out + unused


@dataclass
class EngineState:
    """One provider's engine and its session settings."""

    provider: str
    settings: object = None
    dry_run: bool = True
    configured: float | None = None  # the file's threshold, restored on exit
    adjusting: bool = False
    entry_threshold: float | None = None
    log: AutoLog | None = field(default=None, repr=False)
    buffer: list[str] = field(default_factory=list, repr=False)


class EnginePanel(Vertical):
    """A provider's part of the auto view: active card, badge, Next best."""

    def __init__(self, provider: str) -> None:
        super().__init__(classes="engine-panel")
        self.provider = provider

    def compose(self) -> ComposeResult:
        yield AccountsPanel(provider=self.provider, active_only=True, id=f"auto-card-{self.provider}")
        with Horizontal(classes="auto-title-row"):
            yield Static(" DRY-RUN ", classes="mode-badge dry", id=f"badge-{self.provider}")
            yield Static("", classes="auto-summary", id=f"summary-{self.provider}")
        yield Static("", classes="candidates", id=f"candidates-{self.provider}")


class AutoScreen(Screen):
    BINDINGS = [
        Binding("tab", "next_panel", "Next provider"),
        Binding("l", "toggle_live", "Live / dry run"),
        Binding("t", "adjust_threshold", "Threshold"),
        Binding("left", "threshold_step(-1)", "-1%"),
        Binding("right", "threshold_step(1)", "+1%"),
        Binding("enter", "adjust_done", "Done"),
        Binding("escape", "back", "Back"),
        Binding("q", "app.quit", "Quit"),
    ]

    app: "QswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._states: dict[str, EngineState] = {}
        self._selected: str = "claude"
        self._providers: list[str] = []
        # The menu bar's host: one engine per provider, each in a thread.
        self._supervisor = EngineSupervisor(self._make_engine, self._engine_event)

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        fleet = self.app.fleet
        providers = [v.provider for v in fleet.views if v.accounts] if fleet else ["claude"]
        self._providers = providers or ["claude"]
        self._selected = self._providers[0]
        yield Static("", id="auto-tabs")
        with Horizontal(id="auto-panels"):
            for provider in self._providers:
                yield EnginePanel(provider)
        yield RichLog(id="event-log", highlight=False, markup=False, wrap=False, min_width=20)
        yield Footer()

    @property
    def both(self) -> bool:
        return len(self._providers) > 1

    @property
    def wide(self) -> bool:
        return self.both and self.size.width >= WIDE

    def on_resize(self, _event) -> None:
        self._layout()

    def _layout(self) -> None:
        tabs = self.query_one("#auto-tabs", Static)
        tabs.display = self.both and not self.wide
        for panel in self.query(EnginePanel):
            panel.display = self.wide or panel.provider == self._selected or not self.both
            panel.set_class(self.wide and panel.provider == self._selected, "selected")
        self._update_tabs()
        self.refresh_bindings()

    def _update_tabs(self) -> None:
        if not self.both:
            return
        palette = Palette.from_theme(self.app.current_theme)
        text = Text(no_wrap=True)
        for i, provider in enumerate(self._providers):
            if i:
                text.append("   ")
            state = self._states.get(provider)
            running = self._supervisor.running(provider)
            live = running and state is not None and not state.dry_run
            selected = provider == self._selected
            name = f" {display.provider_name(provider)} "
            text.append(name, style=f"bold {palette.foreground} reverse" if selected else palette.muted)
            mode = " LIVE" if live else (" dry run" if running else " off")
            text.append(mode, style=palette.accent if live else palette.muted)
        text.append("    tab switches", style=palette.muted)
        self.query_one("#auto-tabs", Static).update(text)

    # -- lifecycle ----------------------------------------------------------

    def on_mount(self) -> None:
        self.app.set_store_only(True)
        for provider in self._providers:
            sw = self.app.switcher_for(provider)
            settings = load_engine_settings(data.settings_root(sw), provider) if sw else None
            state = EngineState(provider, settings=settings,
                                configured=settings.threshold if settings else None)
            self._states[provider] = state
            self._update_summary(provider)
        self.watch(self.app, "fleet", self._on_fleet)
        self.watch(self.app, "theme", self._on_theme_change)
        for provider in self._providers:
            self._start_engine(provider, dry_run=True)
        self._layout()

    def on_unmount(self) -> None:
        self._supervisor.stop_all()
        for provider in self._states:
            sw = self.app.switcher_for(provider)
            if sw is not None:
                # A session threshold must not outlive the engine it steered.
                sw.clear_poll_policy_inputs()
            self.app.set_session_threshold(provider, None)
        self.app.set_store_only(False)

    def _on_theme_change(self, _theme: str) -> None:
        for provider in self._providers:
            self._update_summary(provider)
            self._update_badge(provider)
        self._update_tabs()
        self._on_fleet(self.app.fleet)

    def action_back(self) -> None:
        state = self._states.get(self._selected)
        if state is not None and state.adjusting:
            self._end_adjust(self._selected)
            return
        self.app.pop_screen()

    def action_next_panel(self) -> None:
        if not self.both:
            return
        state = self._states.get(self._selected)
        if state is not None and state.adjusting:
            self._end_adjust(self._selected)
        i = self._providers.index(self._selected)
        self._selected = self._providers[(i + 1) % len(self._providers)]
        self._layout()

    # -- threshold adjust mode ------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        state = self._states.get(self._selected)
        adjusting = state is not None and state.adjusting
        if action in ("threshold_step", "adjust_done") and not adjusting:
            return False  # hidden and inert until adjust mode is armed
        if action == "next_panel" and not self.both:
            return False
        return True

    def action_adjust_threshold(self) -> None:
        state = self._states[self._selected]
        if state.adjusting:
            self._end_adjust(self._selected)
            return
        state.adjusting = True
        state.entry_threshold = state.settings.threshold
        self._update_summary(self._selected)
        self.refresh_bindings()

    def action_adjust_done(self) -> None:
        if self._states[self._selected].adjusting:
            self._end_adjust(self._selected)

    def action_threshold_step(self, delta: float) -> None:
        state = self._states[self._selected]
        if not state.adjusting:
            return
        key = "codex.threshold" if state.provider == "codex" else "autoswitch.threshold"
        spec = SETTING_SPECS[key]
        value = min(spec.hi, max(spec.lo, state.settings.threshold + delta))
        self._set_threshold(state.provider, value)

    def _end_adjust(self, provider: str) -> None:
        state = self._states[provider]
        state.adjusting = False
        self._update_summary(provider)
        self.refresh_bindings()
        if state.settings.threshold == state.entry_threshold:
            return  # no net change: nothing to announce, no tick to force
        engine = self._engine(provider)
        if engine is not None:
            engine.wake()  # show a decision at the new value now
        self._note(provider, f"switch line set to {pct_label(state.settings.threshold)}% "
                             "for this session")

    def _set_threshold(self, provider: str, value: float) -> None:
        state = self._states[provider]
        if value == state.settings.threshold:
            return
        state.settings = replace(state.settings, threshold=value)
        engine = self._engine(provider)
        if engine is not None:
            engine.apply_threshold(value)
        self.app.set_session_threshold(
            provider, None if value == state.configured else value
        )
        self._update_summary(provider)
        # The Next best list ranks by the lines in force, so it follows the
        # session value now rather than at the next snapshot.
        self._on_fleet(self.app.fleet)

    def _update_summary(self, provider: str) -> None:
        state = self._states.get(provider)
        if state is None or state.settings is None:
            return
        palette = Palette.from_theme(self.app.current_theme)
        settings = state.settings
        text = Text(no_wrap=True, overflow="ellipsis")
        if self.both:
            text.append(f"{display.provider_name(provider)} · ", style=palette.foreground)
        text.append(
            f"switch at {pct_label(settings.threshold)}%",
            style=f"bold {palette.accent}" if state.adjusting else palette.foreground,
        )
        if settings.threshold != state.configured:
            text.append(" (this session)", style=palette.muted)
        if settings.weekly_threshold is not None:
            text.append(f" · weekly {pct_label(settings.weekly_threshold)}%")
        text.append(f" · checks every {settings.interval_seconds:.0f}s", style=palette.muted)
        if state.adjusting:
            text.append("   ← → adjust · enter done", style=palette.muted)
        self.query_one(f"#summary-{provider}", Static).update(text)

    # -- engines ---------------------------------------------------------------

    def _log_width(self) -> int:
        width = (self.query_one("#event-log", RichLog).size.width or self.app.size.width) - 3
        return max(40, width - (TAG_WIDTH if self.both else 0))

    def _make_engine(self, provider: str, emit) -> AutoSwitchEngine:
        """The supervisor's factory: ``provider``'s engine over its switcher,
        with this view's session settings and mode. Raising is a failed start
        (a Codex home that keeps its login in the keyring, say)."""
        state = self._states[provider]
        sw = self.app.switcher_for(provider)
        if sw is None or state.settings is None:
            raise RuntimeError(f"{display.provider_name(provider)} is not available here")
        if provider == "codex" and hasattr(sw, "require_accounts"):
            sw.require_accounts()  # one clear line instead of an idle engine
        buffer: list[str] = []
        log = AutoLog(buffer.append, provider=provider, width=self._log_width())
        engine = AutoSwitchEngine(
            sw, state.settings,
            lambda event, lg=log, buf=buffer: emit((lg, buf, event)),
            dry_run=state.dry_run,
        )
        engine.host_label = "qswap dashboard"
        state.log, state.buffer = log, buffer
        return engine

    def _engine(self, provider: str):
        return self._supervisor.engine(provider)

    def _start_engine(self, provider: str, *, dry_run: bool) -> None:
        state = self._states[provider]
        state.dry_run = dry_run
        failure = self._supervisor.start(provider)
        self._update_badge(provider)
        self._update_tabs()
        if failure:
            self._note(provider, f"{display.provider_name(provider)} auto-switch did not start: {failure}")
            return
        mode = ("dry run: watching only, switching nothing" if dry_run
                else f"live: switches accounts at {pct_label(state.settings.threshold)}%")
        self._note(provider, f"{display.provider_name(provider)} auto-switch {mode}")

    def _note(self, provider: str, message: str) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        text = Text(no_wrap=True)
        if self.both:
            text.append(f"{PROVIDER_SHORT.get(provider, provider):<{TAG_WIDTH}}", style=palette.muted)
        text.append(f"{data.clock_stamp()}  — {message} —", style=palette.muted)
        width = self._log_width() + (TAG_WIDTH if self.both else 0)
        if len(text.plain) > width:
            text.truncate(width, overflow="ellipsis")
        self.query_one("#event-log", RichLog).write(text)

    def _engine_event(self, provider: str, payload) -> None:
        """The supervisor's ``on_event``, on an engine's thread: the command
        line's log turns an engine event into lines, the UI thread writes
        them. A thread that ended arrives as an ``EngineStoppedEvent``."""
        if isinstance(payload, EngineStoppedEvent):
            self._to_ui(self._on_engine_stopped, provider, payload)
            return
        log, buffer, event = payload
        with printer.force_color():
            log(event)
        lines, buffer[:] = list(buffer), []
        self._to_ui(self._on_engine_lines, provider, event, lines)

    def _to_ui(self, fn, *args) -> None:
        try:
            self.app.call_from_thread(fn, *args)
        except Exception:
            pass  # app/screen tearing down mid-tick; the lines have nowhere to go

    def _on_engine_stopped(self, provider: str, event: EngineStoppedEvent) -> None:
        if not self.is_attached or not self._supervisor.stopped(provider, event):
            return  # a predecessor, or one this view stopped itself
        self._update_badge(provider)
        self._update_tabs()
        self._note(provider, f"{display.provider_name(provider)} auto-switch stopped: {event.human()}")

    def _on_engine_lines(self, provider: str, event: AutoSwitchEvent | None, lines: list[str]) -> None:
        if not self.is_attached:
            return
        log = self.query_one("#event-log", RichLog)
        palette = Palette.from_theme(self.app.current_theme)
        tag = f"{PROVIDER_SHORT.get(provider, provider):<{TAG_WIDTH}}" if self.both else ""
        for line in lines:
            text = Text(no_wrap=True)
            if tag:
                text.append(" " * TAG_WIDTH if line.startswith(" ") else tag, style=palette.muted)
            text.append_text(from_printer(line, palette))
            log.write(text)
        if event is not None and event.kind in ("switch", "limit-reset"):
            self.app.request_refresh()

    def action_toggle_live(self) -> None:
        state = self._states.get(self._selected)
        if state is None:
            return
        name = display.provider_name(state.provider)
        if state.dry_run:
            self.app.push_screen(
                ConfirmModal(
                    f"Go live for {name}? qswap switches the active {name} account "
                    f"when it reaches {pct_label(state.settings.threshold)}%.\n\n"
                    f"The same as running {'qswap codex auto' if state.provider == 'codex' else 'qswap auto'} "
                    "in a terminal, until you leave this view.",
                    title="Go live",
                    yes_label="Go live",
                ),
                lambda yes, p=state.provider: self._restart_engine(p, dry_run=False) if yes else None,
            )
        else:
            self._restart_engine(state.provider, dry_run=True)

    def _restart_engine(self, provider: str, *, dry_run: bool) -> None:
        state = self._states[provider]
        if state.log is not None:
            # A check still waiting for its decision prints before the switch.
            with printer.force_color():
                state.log.flush()
            lines, state.buffer[:] = list(state.buffer), []
            self._on_engine_lines(provider, None, lines)
        self._supervisor.stop(provider)
        self._start_engine(provider, dry_run=dry_run)

    def _update_badge(self, provider: str) -> None:
        state = self._states.get(provider)
        badge = self.query_one(f"#badge-{provider}", Static)
        if not self._supervisor.running(provider):
            badge.update(" OFF ")
            badge.set_classes("mode-badge off")
            return
        live = state is not None and not state.dry_run
        badge.update(" LIVE " if live else " DRY-RUN ")
        badge.set_classes("mode-badge " + ("live" if live else "dry"))

    # -- candidates -----------------------------------------------------------

    def _on_fleet(self, fleet: data.Fleet | None) -> None:
        if fleet is None or not self.is_mounted:
            return
        for provider in self._providers:
            view = fleet.view(provider)
            if view is None:
                continue
            widget = self.query_one(f"#candidates-{provider}", Static)
            widget.update(self._candidates_text(view, widget.size.width or self.app.size.width - 4))

    def _candidates_text(self, view: data.ProviderView, width: int) -> Text:
        palette = Palette.from_theme(self.app.current_theme)
        state = self._states.get(view.provider)
        settings = state.settings if state else None
        rows = next_best(view, settings, threshold=settings.threshold if settings else None)
        text = Text(no_wrap=True)
        text.append("Next best", style=palette.muted)
        if not rows:
            text.append("\n  no other account the engine would switch to", style=palette.muted)
            return text
        name_width = min(28, max(len(r.label) for r in rows))
        for row in rows:
            line = Text(no_wrap=True, overflow="ellipsis")
            line.append("  ")
            line.append(display.fit(row.label, name_width).ljust(name_width), style=palette.foreground)
            line.append("  ")
            colour = level_colour(row.level, palette) if row.level != "dim" else palette.muted
            line.append(row.words, style=colour)
            if row.line:
                line.append(f" {row.line}", style=palette.muted)
            if len(line.plain) > width:
                line.truncate(max(10, width), overflow="ellipsis")
            text.append("\n")
            text.append_text(line)
        return text


__all__ = ["AutoScreen", "Candidate", "event_text", "header_text", "next_best"]
