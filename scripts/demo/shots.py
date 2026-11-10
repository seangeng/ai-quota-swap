"""The README and docs shot list: which scenario, which command, what size.

CLI shots render to HTML (``docs/shots/<name>.html`` and ``<name>-light.html``)
and are screenshotted from there; TUI shots are screenshotted from the served
dashboard. ``columns`` is the terminal's column count: a CLI window is as wide
as its columns at one font size, and a TUI shot is sized in cells (columns x
rows), so text keeps one scale across the README. Everything is captured at 2x.
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
)


def page_names() -> list[str]:
    out = []
    for s in CLI_SHOTS:
        out += [s.name if t == "dark" else f"{s.name}-{t}" for t in s.themes]
    out += [s.name for s in TUI_SHOTS]
    return out
