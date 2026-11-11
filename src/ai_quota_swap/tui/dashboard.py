"""Dashboard: the accounts on top, a nested action menu right below them.

The accounts panel is the monitor: each provider's accounts under its own
heading (headings only when both providers have accounts), the active account
in full, the others on one line each. The arrow keys drive the *menu*, not the
accounts. Anything account-targeted opens a context of its own:

- ``s`` / menu "Switch account" → :class:`SwitchScreen` — every account in
  full, both providers in sections; Enter switches that account's provider
  and pops back.
- ``w`` / menu "Watch accounts" / ``qswap watch`` → :class:`WatchScreen` —
  the same cards, read-only: a live monitor. ``s`` arms selection, Enter
  switches and *stays watching*, Esc disarms.
- "Disable / enable" and "Remove" nest into a submenu listing the accounts.

Keys mean one thing everywhere: ``esc`` goes back, ``q`` quits, ``s``
switches, ``w`` watches, ``g`` opens auto-switch, ``u`` the usage report.
No menu entry offers an action that cannot work.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, ListView, Static

from ai_quota_swap import display
from ai_quota_swap.tui.data import Fleet
from ai_quota_swap.tui.widgets import (
    AccountItem,
    AccountsPanel,
    HeadingItem,
    MenuItem,
    label_width_for,
)

if TYPE_CHECKING:
    from ai_quota_swap.tui.app import QswapApp

FLASH_S = 1.5  # how long a just-refreshed row stays highlighted

MenuEntries = list[tuple[str, str]]  # (label, action_id); action "heading" is a heading

_BACK = ("← back", "back")

#: The root menu, every entry, in order (a first run shows fewer: an action
#: that cannot work is not offered). scripts/demo/tour.py walks it by index.
ROOT_MENU: MenuEntries = [
    ("Switch account…", "switch"),
    ("Watch accounts", "watch"),
    ("Auto-switch view", "auto"),
    ("Usage report", "usage"),
    ("Add account…", "add-menu"),
    ("Disable / enable account…", "disable-menu"),
    ("Remove account…", "remove-menu"),
    ("Theme…", "theme-menu"),
    ("Quit", "quit"),
]


def _has_switch_target(fleet: Fleet | None) -> bool:
    return bool(fleet) and any(not acc.is_active for _v, acc in fleet.rows())


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("s", "open_switch", "Switch"),
        Binding("w", "open_watch", "Watch"),
        Binding("g", "open_auto", "Auto-switch"),
        Binding("u", "open_usage", "Usage"),
        Binding("escape,left", "menu_back", "Back"),
        Binding("q", "app.quit", "Quit"),
        Binding("f", "app.refresh_full", "Refresh usage", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    app: "QswapApp"

    def __init__(self) -> None:
        super().__init__()
        # Stack of (title, builder); depth 1 = root menu. A builder rather
        # than a list, so a submenu follows the accounts as they change.
        self._menu_stack: list[tuple[str, object]] = []
        self._shown: MenuEntries = []

    def compose(self) -> ComposeResult:
        yield Static("", id="cost-line")
        yield AccountsPanel(id="accounts-panel")
        yield Static("", id="menu-title")
        yield ListView(id="menu")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#menu", ListView).focus()
        self.watch(self.app, "fleet", self._on_fleet)
        self.watch(self.app, "cost", self._on_cost)
        await self._push_menu("menu", self._root_entries)

    # -- menu entries -----------------------------------------------------------

    def _root_entries(self) -> MenuEntries:
        fleet = self.app.fleet if self is not None else None
        if fleet is None:
            return list(ROOT_MENU)
        entries: MenuEntries = []
        unmanaged = [v for v in fleet.views if v.live_email and v.active_number is None]
        for view in unmanaged:
            # The first step on a first run: the live login, one key away.
            label = f"Add {view.live_email}"
            if len(unmanaged) > 1 or fleet.managed:
                label += f" · {view.name}"
            entries.append((label, f"add-login:{view.provider}"))
        offered = {
            "switch": _has_switch_target(fleet),
            "watch": fleet.managed,
            "auto": fleet.managed,
            "disable-menu": fleet.managed,
            "remove-menu": fleet.managed,
        }
        entries.extend(e for e in ROOT_MENU if offered.get(e[1], True))
        return entries

    def _add_entries(self) -> MenuEntries:
        """What can be added, per provider: only what would work now."""
        app = self.app
        entries: MenuEntries = []
        both = "codex" in app.switchers
        if both:
            entries.append(("Claude Code", "heading"))
        email = app.live_email("claude")
        if email:
            entries.append((f"From the current login ({email})", "add-login:claude"))
        entries.append(("From a setup-token or API key…", "add-token"))
        if both:
            entries.append(("Codex", "heading"))
            codex_email = app.live_email("codex")
            if codex_email:
                entries.append((f"From the current Codex login ({codex_email})", "add-login:codex"))
            view = app.fleet.view("codex") if app.fleet else None
            if view is None or view.cli_found is not False:
                entries.append(("Log in another account…", "codex-login"))
        entries.append(_BACK)
        return entries

    def _account_entries(self, kind: str) -> MenuEntries:
        """One row per account, by provider; ``kind`` is ``remove`` or ``disable``."""
        fleet = self.app.fleet
        entries: MenuEntries = []
        if fleet is None:
            return [_BACK]
        for view in fleet.views:
            if not view.accounts:
                continue
            if fleet.sectioned:
                entries.append((display.provider_heading(view.provider, both=True), "heading"))
            for acc in view.accounts:
                name = display.account_label(acc.number, alias=acc.alias or None,
                                             email=acc.email, long=True)
                if kind == "disable":
                    state = " · disabled" if acc.disabled else ""
                    action = "→ enable" if acc.disabled else "→ disable"
                    label = f"{name}{state}   {action}"
                else:
                    state = (
                        " · active" if acc.is_active
                        else " · underneath" if getattr(acc, "underneath", False) else ""
                    )
                    label = f"{name}{state}"
                entries.append((label, f"{kind}:{view.provider}:{acc.number}"))
        entries.append(_BACK)
        return entries

    def _theme_entries(self) -> MenuEntries:
        """dark / light / auto, with the active setting marked."""
        current = self.app._theme_name
        entries: MenuEntries = [
            (f"{'●' if name == current else ' '} {name}", f"theme:{name}")
            for name in ("dark", "light", "auto")
        ]
        entries.append(_BACK)
        return entries

    # -- menu plumbing --------------------------------------------------------

    async def _push_menu(self, title: str, builder) -> None:
        self._menu_stack.append((title, builder))
        await self._render_menu(reset=True)

    async def _pop_menu(self) -> None:
        if len(self._menu_stack) > 1:
            self._menu_stack.pop()
            await self._render_menu(reset=True)

    async def _render_menu(self, *, reset: bool) -> None:
        _title, builder = self._menu_stack[-1]
        entries = builder()
        crumb = " › ".join(t for t, _ in self._menu_stack)
        title = self.query_one("#menu-title", Static)
        title.update(crumb)
        # At the root the cursor says it is a menu; a submenu says where it is.
        title.display = len(self._menu_stack) > 1
        menu = self.query_one("#menu", ListView)
        if entries == self._shown and not reset:
            return
        selected = None
        if not reset and menu.index is not None and menu.index < len(self._shown):
            selected = self._shown[menu.index][1]
        self._shown = entries
        await menu.clear()
        await menu.extend(
            MenuItem(label, action_id, muted=(action_id == "back"), heading=(action_id == "heading"))
            for label, action_id in entries
        )
        ids = [a for _l, a in entries]
        if selected in ids:
            menu.index = ids.index(selected)
        else:
            menu.index = next((i for i, a in enumerate(ids) if a != "heading"), 0)
        self.refresh_bindings()
        self._fit()

    async def _on_fleet(self, _fleet) -> None:
        if self._menu_stack:
            await self._render_menu(reset=False)
        signature = (_has_switch_target(self.app.fleet), bool(self.app.fleet and self.app.fleet.managed))
        if signature != getattr(self, "_signature", None):
            self._signature = signature
            self.refresh_bindings()
        self._fit()

    def _on_cost(self, cost) -> None:
        line = self.query_one("#cost-line", Static)
        line.update(cost.text() if cost is not None else "")
        line.display = cost is not None
        self._fit()

    def on_resize(self, _event) -> None:
        self._fit()

    def _fit(self) -> None:
        """Share the height: the menu shows whole when it can, the accounts
        take the rest (folding rows past it), and nothing scrolls by surprise."""
        if not self.is_mounted:
            return
        height = self.size.height
        if not height:
            return
        menu = self.query_one("#menu", ListView)
        panel = self.query_one(AccountsPanel)
        cost = 1 if self.query_one("#cost-line", Static).display else 0
        crumb = 1 if self.query_one("#menu-title", Static).display else 0
        # The footer, the cost line, the panel's top padding, the blank line
        # above the menu and its title in a submenu.
        fixed = 1 + cost + 1 + 1 + crumb
        entries = len(self._shown)
        min_panel = 6
        menu_rows = min(entries, max(4, height - fixed - min_panel))
        menu.styles.height = max(1, menu_rows)
        panel.set_max_rows(max(3, height - fixed - menu_rows))

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, MenuItem) and item.action_id != "heading":
            await self._dispatch(item.action_id)

    async def _dispatch(self, action_id: str) -> None:
        app = self.app
        simple = {
            "switch": self.action_open_switch,
            "watch": app.action_open_watch,
            "auto": app.action_open_auto,
            "usage": app.action_open_usage,
            "add-token": app.action_add_token,
            "codex-login": app.codex_login,
            "quit": app.exit,
        }
        if action_id == "back":
            await self._pop_menu()
        elif action_id in simple:
            simple[action_id]()
        elif action_id == "add-menu":
            await self._push_menu("add account", self._add_entries)
        elif action_id.startswith("add-login:"):
            app.add_current(action_id.split(":", 1)[1])
        elif action_id == "remove-menu":
            await self._push_menu("remove account", partial(self._account_entries, "remove"))
        elif action_id.startswith("remove:"):
            _kind, provider, number = action_id.split(":", 2)
            app.confirm_remove(provider, number)
        elif action_id == "disable-menu":
            await self._push_menu("disable / enable", partial(self._account_entries, "disable"))
        elif action_id.startswith("disable:"):
            _kind, provider, number = action_id.split(":", 2)
            app.do_toggle_disabled(provider, number)
            await self._pop_menu()
        elif action_id == "theme-menu":
            await self._push_menu("theme", self._theme_entries)
        elif action_id.startswith("theme:"):
            name = action_id.split(":", 1)[1]
            app.apply_theme(name)
            app.notify(f"Theme: {name}")
            await self._pop_menu()

    # -- actions ----------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        fleet = self.app.fleet
        if action == "menu_back":
            return len(self._menu_stack) > 1
        if action == "open_switch":
            return _has_switch_target(fleet)
        if action in ("open_watch", "open_auto"):
            return bool(fleet and fleet.managed)
        return True

    def action_open_watch(self) -> None:
        self.app.action_open_watch()

    def action_open_auto(self) -> None:
        self.app.action_open_auto()

    def action_open_usage(self) -> None:
        self.app.action_open_usage()

    def action_open_switch(self) -> None:
        if not isinstance(self.app.screen, SwitchScreen) and _has_switch_target(self.app.fleet):
            self.app.push_screen(SwitchScreen())

    async def action_menu_back(self) -> None:
        await self._pop_menu()

    def action_cursor_down(self) -> None:
        self.query_one("#menu", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#menu", ListView).action_cursor_up()


class AccountListScreen(Screen):
    """Shared machinery: a live ListView of full account cards, both
    providers in display order, a heading over each when both have accounts.

    Subclasses decide what the cursor does — :class:`SwitchScreen` is
    selection-first, :class:`WatchScreen` is a monitor that can arm
    selection on demand.
    """

    app: "QswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._keys: list[tuple[str, str]] = []
        self._stamps: dict[tuple[str, str], float | None] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="list-title")
        yield ListView(id="accounts")
        yield Footer()

    def on_mount(self) -> None:
        self.watch(self.app, "fleet", self._on_fleet)

    @staticmethod
    def _layout(fleet: Fleet) -> list[tuple[str, str]]:
        keys: list[tuple[str, str]] = []
        for view in fleet.views:
            if not view.accounts:
                continue
            if fleet.sectioned:
                keys.append(("heading", view.provider))
            keys.extend((view.provider, acc.number) for acc in view.accounts)
        return keys

    async def _on_fleet(self, fleet: Fleet | None) -> None:
        if fleet is None:
            return
        listview = self.query_one("#accounts", ListView)
        keys = self._layout(fleet)
        labels = label_width_for(fleet)
        if keys != self._keys:
            first_build = not self._keys
            previous = self._current_key()
            await listview.clear()
            items = []
            for key in keys:
                if key[0] == "heading":
                    items.append(HeadingItem(display.provider_heading(key[1], both=True)))
                else:
                    view, acc = fleet.find(*key)  # type: ignore[misc]
                    items.append(AccountItem(view, acc, label_width=labels))
            await listview.extend(items)
            self._keys = keys
            listview.index = self._index_after_build(fleet, first_build, previous)
        else:
            for item in listview.query(AccountItem):
                found = fleet.find(item.provider, item.number)
                if found is not None:
                    item.set_account(*found, label_width=labels)
        self._flash_updated(fleet, listview)

    def _current_key(self) -> tuple[str, str] | None:
        listview = self.query_one("#accounts", ListView)
        if listview.index is None or listview.index >= len(self._keys):
            return None
        return self._keys[listview.index]

    def _index_after_build(
        self, fleet: Fleet, first_build: bool, previous: tuple[str, str] | None
    ) -> int | None:
        """Where the cursor lands after the list is (re)built."""
        if not first_build and previous in self._keys:
            return self._keys.index(previous)
        return self._active_index(fleet)

    def _active_index(self, fleet: Fleet, provider: str | None = None) -> int | None:
        """The active account's row: ``provider``'s, else the first provider's."""
        for view in fleet.views:
            if provider is not None and view.provider != provider:
                continue
            if view.active_number is not None and (view.provider, view.active_number) in self._keys:
                return self._keys.index((view.provider, view.active_number))
        return next((i for i, k in enumerate(self._keys) if k[0] != "heading"), None)

    def _flash_updated(self, fleet: Fleet, listview: ListView) -> None:
        """Briefly highlight rows whose stored measurement just advanced."""
        new_stamps = {(v.provider, acc.number): acc.usage.fetched_at for v, acc in fleet.rows()}
        if self._stamps:
            changed = {
                key
                for key, ts in new_stamps.items()
                if ts is not None and ts != self._stamps.get(key)
            }
            for item in listview.query(AccountItem):
                if item.key in changed and not item.has_class("flash"):
                    item.add_class("flash")
                    self.set_timer(FLASH_S, partial(item.remove_class, "flash"))
        self._stamps = new_stamps

    def highlighted(self) -> AccountItem | None:
        listview = self.query_one("#accounts", ListView)
        item = listview.highlighted_child
        return item if isinstance(item, AccountItem) else None

    def action_cursor_down(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_up()


class SwitchScreen(AccountListScreen):
    """All accounts, in full and alive: arrows pick, Enter switches."""

    BINDINGS = [
        # priority: outranks the focused ListView's own (hidden) enter binding
        # so "Switch" is visible in the footer; the action delegates right back
        # to the list cursor, so behavior is identical.
        Binding("enter", "select_highlighted", "Switch", priority=True),
        Binding("b", "best_pick", "Best pick"),
        Binding("escape", "back", "Back"),
        Binding("q", "app.quit", "Quit"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def on_mount(self) -> None:
        self.query_one("#list-title", Static).update("Switch to which account?")
        self.query_one("#accounts", ListView).focus()
        super().on_mount()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, AccountItem):
            self.app.do_switch(item.provider, item.number)
            self.app.pop_screen()

    def action_select_highlighted(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if listview.display:
            listview.action_select_cursor()

    def action_best_pick(self) -> None:
        """The account with the most quota left, in the highlighted account's
        provider (``qswap switch --strategy best``)."""
        item = self.highlighted()
        provider = item.provider if item is not None else "claude"
        self.app.switch_best(provider)
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()


class WatchScreen(AccountListScreen):
    """Live monitor of every account, full detail, hands-off by default.

    ``s`` arms selection (cursor appears on the active account); Enter then
    switches and stays here — you keep watching on the new account. Esc
    disarms selection first, then leaves the screen.
    """

    _WATCH_TITLE = "Watching all accounts"
    _SELECT_TITLE = "Switch to which account? · enter switches · esc cancels"

    BINDINGS = [
        Binding("s", "toggle_select", "Switch"),
        Binding("enter", "select_highlighted", "Switch", priority=True),
        Binding("f", "app.refresh_full", "Refresh", show=False),
        Binding("escape", "back", "Back"),
        Binding("q", "app.quit", "Quit"),
        Binding("down,j", "nav_down", show=False),
        Binding("up,k", "nav_up", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._armed = False

    def on_mount(self) -> None:
        self.watch(self.app, "refresh_status", self._on_refresh_status)
        self.query_one("#list-title", Static).update(self._title_text())
        super().on_mount()

    def _title_text(self) -> str:
        if self._armed:
            return self._SELECT_TITLE
        status = self.app.refresh_status
        return f"{self._WATCH_TITLE} · {status}" if status else self._WATCH_TITLE

    def _on_refresh_status(self, status: str) -> None:
        if not self._armed:
            self.query_one("#list-title", Static).update(self._title_text())

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "select_highlighted" and not self._armed:
            return False  # hidden and inert until selection is armed
        if action == "toggle_select" and self._armed:
            return False
        return True

    def _index_after_build(
        self, fleet: Fleet, first_build: bool, previous: tuple[str, str] | None
    ) -> int | None:
        if not self._armed:
            return None  # monitor mode: no cursor at all
        return super()._index_after_build(fleet, first_build, previous)

    def _set_selecting(self, on: bool) -> None:
        self._armed = on
        listview = self.query_one("#accounts", ListView)
        title = self.query_one("#list-title", Static)
        if on:
            fleet = self.app.fleet
            if fleet is not None and self._keys:
                listview.index = self._active_index(fleet)
            listview.focus()
            title.update(self._SELECT_TITLE)
        else:
            listview.index = None
            self.set_focus(None)
            title.update(self._title_text())
        self.refresh_bindings()

    def action_toggle_select(self) -> None:
        self._set_selecting(not self._armed)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not self._armed:
            return  # e.g. a stray click while just watching
        item = event.item
        if isinstance(item, AccountItem):
            self.app.do_switch(item.provider, item.number)
            self._set_selecting(False)  # stay here, keep watching

    def action_select_highlighted(self) -> None:
        if self._armed:
            self.query_one("#accounts", ListView).action_select_cursor()

    def action_back(self) -> None:
        if self._armed:
            self._set_selecting(False)
        else:
            self.app.pop_screen()

    def action_nav_down(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if self._armed:
            listview.action_cursor_down()
        else:
            listview.scroll_down(animate=False)

    def action_nav_up(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if self._armed:
            listview.action_cursor_up()
        else:
            listview.scroll_up(animate=False)
