"""The README shot list: which scenario, which command, what size.

CLI shots render to HTML (``docs/shots/<name>.html`` and ``<name>-light.html``)
and are screenshotted from there; TUI shots are screenshotted from the served
dashboard. Width is the terminal's column count; every window is 1200 CSS px
wide and captured at 2x, so text keeps one scale across the README.
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
    view: str  # "dashboard" | "watch"
    caption: str
    font_size: int = 19
    height: int = 600  # px of terminal area; rows = height / cell height
    theme: str = "dark"


CLI_SHOTS = (
    CliShot("list", "fleet", ("list",), 100,
            "qswap list: every account's 5-hour, weekly and per-model usage, plan and resets"),
    CliShot("reset", "reset", ("reset", "++", "reset", "1", "--yes"), 112,
            "qswap reset: the grants each account holds, then one spent on a window at 100%"),
    CliShot("usage", "usage", ("usage",), 112,
            "qswap usage: tokens and estimated API cost per day, Claude Code and Codex"),
    CliShot("usage-by-model", "usage", ("usage", "--by", "model"), 112,
            "qswap usage --by model"),
    CliShot("auto", "autoswitch", ("auto",), 124,
            "qswap auto: the engine watches the active account and switches at the line"),
)

TUI_SHOTS = (
    TuiShot("dashboard", "fleet", "dashboard",
            "The dashboard (qswap): the active account in full, the rest at a glance",
            height=600),
    TuiShot("watch", "fleet", "watch",
            "The watch page (qswap watch): every account's windows, live",
            height=570),
)


def page_names() -> list[str]:
    out = []
    for s in CLI_SHOTS:
        out += [s.name if t == "dark" else f"{s.name}-{t}" for t in s.themes]
    out += [s.name for s in TUI_SHOTS]
    return out
