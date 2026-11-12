"""The README and docs shot list: which scenario, which command, what size.

CLI shots render to HTML (``docs/shots/<name>.html`` and ``<name>-light.html``)
and are screenshotted from there; TUI shots are screenshotted from the served
dashboard; menu bar shots are drawings of the menu's rows
(``docs/shots/<name>.html``, see ``menubar_page.py``), taken after the TUI
shots because the hero puts one behind the menu; chart shots are the menu's
chart alone, drawn by the app's AppKit view (``menubar_chart.py``). ``columns`` is the
terminal's column count: a CLI window is as wide as its columns at one font
size, and a TUI shot is sized in cells (columns x rows), so text keeps one
scale across the README. Everything is captured at 2x.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CliShot:
    name: str
    scenario: str
    args: tuple[str, ...]  # qswap arguments; "++" separates commands in one shot
    columns: int
    caption: str
    themes: tuple[str, ...] = ("dark", "light")


@dataclass(frozen=True)
class TuiShot:
    name: str
    scenario: str
    caption: str
    steps: tuple = ()  # ("key", "Enter") | ("type", "g") | ("wait", seconds), from the first frame
    columns: int = 100
    rows: int = 30
    font_size: int = 18
    settle: float = 4.0  # seconds after the first frame before the first key
    theme: str = "dark"


CLI_SHOTS = (
    CliShot("list", "showcase", ("list",), 80,
            "qswap list: every account's 5-hour, weekly and per-model usage, plan, resets and spend"),
    CliShot("status", "showcase", ("status",), 80,
            "qswap status: the active account of each provider, and whether auto-switch runs"),
    CliShot("reset", "reset", ("reset", "++", "reset", "1", "--yes"), 80,
            "qswap reset: a Fable reset and a weekly reset held, then the Fable one spent"),
    CliShot("usage", "showcase", ("usage",), 80,
            "qswap usage: tokens and estimated API cost per day, Claude Code and Codex"),
    CliShot("usage-by-model", "showcase", ("usage", "--by", "model"), 80,
            "qswap usage --by model"),
    CliShot("auto", "autoswitch", ("auto",), 80,
            "qswap auto: one line per check, and a switch at the line"),
    CliShot("doctor", "empty-claude-codex", ("doctor",), 80,
            "qswap doctor on a first run: what is set up, what is not, and the command that fixes it"),
)

TUI_SHOTS = (
    TuiShot("dashboard", "showcase",
            "The dashboard (qswap): both providers' accounts, the active ones in full, today's cost",
            columns=100, rows=30),
    TuiShot("watch", "showcase",
            "The watch page (w, or qswap watch): every account's windows, live",
            steps=(("type", "w"), ("wait", 1.5)), columns=100, rows=40),
    TuiShot("dashboard-usage", "showcase",
            "The usage screen (u), regrouped by account (Tab, Tab): both providers' accounts",
            steps=(("type", "u"), ("wait", 2.5), ("key", "Tab"), ("key", "Tab"), ("wait", 2.0)),
            columns=100, rows=22),
    TuiShot("dashboard-auto", "showcase",
            "The auto view (g): one engine per provider, side by side on a wide terminal",
            steps=(("type", "g"), ("wait", 3.0)), columns=140, rows=26, font_size=16),
    TuiShot("first-run", "empty-claude-codex",
            "A first run: live Claude Code and Codex logins, nothing managed yet",
            columns=80, rows=16),
    # The light twin, for the light hero (and a <picture> beside dashboard.png).
    # The footer's s / w / g keys appear once the fleet is read; the light
    # theme's start takes longer to get there.
    TuiShot("dashboard-light", "showcase",
            "The dashboard (qswap) in the light theme",
            columns=100, rows=30, theme="light", settle=8.0),
)


@dataclass(frozen=True)
class MenubarShot:
    """A drawing of the macOS menu bar with qswap's menu open
    (``run.py <scenario> --menubar --html``): the rows ``build_menu`` gives
    for the scenario, drawn as the native menu draws them."""

    name: str
    scenario: str
    caption: str
    args: tuple[str, ...] = ()  # qswap commands run first to set the state; "++" between
    theme: str = "dark"
    open: str | None = None  # a submenu shown open beside the menu
    highlight: str | None = None  # a row drawn under the pointer
    auto_switch: str | None = "both"  # the menu bar's own engines, hosted as the app does
    beside: str | None = None  # a TUI shot whose PNG is the window behind the menu


MENUBAR_SHOTS = (
    MenubarShot("menubar", "showcase",
                "The menu bar: both providers' accounts with their usage, the active ones "
                "checked, held resets, the seven-day cost chart, auto-switch running"),
    MenubarShot("menubar-light", "showcase", "The menu bar in the light theme", theme="light"),
    MenubarShot("menubar-resets", "showcase",
                "Use a reset: each held usage reset and Codex reset credit, and whether "
                "it can be used now",
                open="Use a reset", highlight="Codex #2 work"),
    # One submenu level each, so the menus stay at a readable size on the page.
    MenubarShot("menubar-settings", "showcase",
                "Settings: the title, Show usage chart and Refresh interval, the "
                "auto-switch toggle for each provider and its threshold, automatic resets, "
                "and Start at login",
                open="Settings"),
    MenubarShot("menubar-auto-resets", "showcase",
                "Use resets automatically: when the engines spend a usage reset by "
                "themselves, as a last resort by default",
                open="Settings > Use resets automatically"),
    MenubarShot("menubar-key", "showcase",
                "An API key active: today's and this month's spend against its $40 budget, "
                "over #1 personal, which stays logged in underneath",
                args=("switch", "5")),
    MenubarShot("hero", "showcase",
                "The qswap menu bar open over the dashboard: every Claude Code and Codex "
                "account with its usage, held resets and the seven-day cost chart",
                beside="dashboard"),
    MenubarShot("hero-light", "showcase",
                "The qswap menu bar open over the dashboard, light theme",
                theme="light", beside="dashboard-light"),
)


@dataclass(frozen=True)
class ChartShot:
    """The menu bar's usage chart alone, drawn by the app's own AppKit view
    off screen (``menubar_chart.py``) from a scenario's ledger: ``<name>.png``
    (dark) and ``<name>-light.png``. macOS only; a pair for a ``<picture>``."""

    name: str
    scenario: str
    caption: str


CHART_SHOTS = (
    ChartShot("menubar-chart", "usage",
              "The cost row's chart: the last seven days of estimated cost, by account"),
)


def page_names() -> list[str]:
    """The pages ScreenFramed frames: CLI windows and dashboard shots. The
    menu bar drawings bring their own desktop and are not framed."""
    out = []
    for s in CLI_SHOTS:
        out += [s.name if t == "dark" else f"{s.name}-{t}" for t in s.themes]
    out += [s.name for s in TUI_SHOTS]
    return out
