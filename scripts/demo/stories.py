"""Scripted recordings: which keys to press, and when.

One storyboard drives both recorders: LittleDemo (``to_littledemo`` writes
its ``steps.json``) and the local fallback (``cdp.record_frames`` plus
ffmpeg). There are two kinds:

* the dashboard, served on loopback (``run.py --serve``): keys go into the
  TUI through xterm.js in the served page, printable keys as typed text into
  its helper textarea, Enter/Escape as key presses;
* a page the harness writes and plays (``page``): ``typing`` is
  ``run.py --typing`` (real commands typed and answered, ``typing_page.py``),
  ``menubar`` plays the menu bar's drawings after each engine check
  (``player_page.py``). The page holds its first frame until a key press,
  then follows its own timeline; the storyboard is that key and a pause as
  long as the page plays (``capture.py`` reads it from the page).

Timing assumes the scenario's ``AutoRun.compression``: with 20x, each
one-minute engine tick takes three seconds of video.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

XTERM_INPUT = ".xterm-helper-textarea"
READY = "body.-first-byte"  # textual.js sets it once the app has drawn
PAGE_READY = "body.ready"  # a played page, once its first frame is drawn


@dataclass(frozen=True)
class Story:
    name: str
    scenario: str
    title: str
    prompt: str  # what the recording shows, for the LittleDemo upload
    steps: tuple[tuple[str, object], ...] = ()  # ("pause", ms) | ("type", "g") | ("key", "Enter")
    view: str = "dashboard"
    # The dashboard's terminal: its font size in px, and either its size in
    # cells (fitted the way tour.py fits them) or in px.
    font: int = 18
    cells: tuple[int, int] | None = None
    term: tuple[int, int] = (1176, 620)
    # A page the harness writes and plays instead: "typing" or "menubar".
    page: str | None = None
    args: tuple[str, ...] = ()  # "typing": the qswap commands, "++" between
    # Video cut from the start of the GIF: a dashboard still drawing its
    # footer is not a first frame.
    trim_ms: int = 0

    @property
    def ready(self) -> str:
        return PAGE_READY if self.page else READY


AUTOSWITCH = Story(
    name="autoswitch",
    scenario="autoswitch",
    title="qswap: auto-switch moves to the best account at the limit",
    prompt=(
        "The qswap dashboard with synthetic accounts: open the auto-switch view, go live, "
        "and watch the active account climb to its 90% line and switch to the account with "
        "the most headroom."
    ),
    steps=(
        ("pause", 2000),
        ("type", "g"),  # auto-switch view (opens in dry-run)
        ("pause", 2000),
        ("type", "l"),  # go live...
        ("pause", 1200),
        ("type", "y"),  # ...confirmed; the switch comes about 3 s later
        ("pause", 6000),
    ),
)

RESET = Story(
    name="reset",
    scenario="reset",
    title="qswap: a usage reset refills a per-model window",
    prompt=(
        "The qswap dashboard with synthetic accounts: the active account is at 100% on its "
        "Fable weekly window and holds a usage reset; the auto engine goes live and spends "
        "it, and the window refills to 0%."
    ),
    trim_ms=1000,  # the footer's keys appear once the fleet is read
    steps=(
        ("pause", 2400),
        ("type", "g"),
        ("pause", 1800),
        ("type", "l"),
        ("pause", 1000),
        ("type", "y"),  # the first live check spends the reset
        ("pause", 4500),
        ("key", "Escape"),  # back to the dashboard, window refilled
        ("pause", 2200),
    ),
)

DASHBOARD = Story(
    name="dashboard",
    scenario="showcase",
    title="qswap: the dashboard's main view, watch, auto-switch and usage",
    prompt=(
        "The qswap dashboard with synthetic accounts: the main view with Claude Code and "
        "Codex accounts, then the watch page (w), the auto-switch view (g) and the usage "
        "report (u), each closed with esc."
    ),
    # 40 rows: the watch page shows every account of the showcase at once.
    cells=(100, 40),
    # The footer's s / w / g keys appear once the showcase is read.
    trim_ms=1000,
    # LittleDemo adds 500 ms after each typed key and 1.3 s after each
    # pressed one: each view stays about 2.9 s, the main view 1.3 s between.
    # Kept short: the dashboard's clock runs in real time while the recorder
    # plays at a third of it, and past a minute the readings would age into
    # "as of 3m ago".
    steps=(
        ("pause", 2400),
        ("type", "w"),  # watch: every account's windows
        ("pause", 2400),
        ("key", "Escape"),
        ("type", "g"),  # the auto-switch view, in dry run
        ("pause", 2400),
        ("key", "Escape"),
        ("type", "u"),  # the usage report
        ("pause", 2400),
        ("key", "Escape"),  # back on the main view
        ("pause", 300),
    ),
)

QUICKSTART = Story(
    name="quickstart",
    scenario="quickstart",
    title="qswap: save a second account, list both, switch back, check",
    prompt=(
        "A terminal with synthetic accounts: #1 personal is saved and Claude Code is logged "
        "in to a second account. qswap add saves it as #2 work, qswap list shows both, "
        "qswap switch 1 moves back to #1 personal, and qswap status confirms it."
    ),
    page="typing",
    # `switch 1`: the account just added is the active one.
    args=("add", "--alias", "work", "++", "list", "++", "switch", "1", "++", "status"),
)

MENUBAR = Story(
    name="menubar",
    scenario="autoswitch",
    title="qswap menu bar: auto-switch moves on before the limit",
    prompt=(
        "The macOS menu bar with synthetic accounts, drawn from the menu bar's own rows: "
        "the title's 5-hour figure for #1 personal climbs from 86% to 88%, the menu bar's "
        "auto-switch moves to #2 work at 91%, and the menu opens on the switch history."
    ),
    page="menubar",
)

STORIES = {s.name: s for s in (AUTOSWITCH, RESET, DASHBOARD, QUICKSTART, MENUBAR)}


def duration_ms(story: Story) -> int:
    return sum(int(v) for k, v in story.steps if k == "pause") + 900 * sum(
        1 for k, _ in story.steps if k != "pause"
    )


# What a LittleDemo key step takes after the key goes down: 800 ms of its key
# overlay, then its default 500 ms delay before the next step.
LITTLEDEMO_KEY_MS = 1300


def page_steps(duration: int, *, key_ms: int = 0) -> tuple[tuple[str, object], ...]:
    """A played page's storyboard: the key that starts it, then the rest of
    its length (``key_ms`` of it pass inside the key step)."""
    return (("key", "Enter"), ("pause", max(0, duration - key_ms)))


def to_littledemo(story: Story, path: Path, *, viewport: tuple[int, int],
                  steps: tuple | None = None) -> Path:
    """Write the story as a LittleDemo ``steps.json`` (``record --steps``)."""
    out: list[dict] = [{"action": "wait", "selector": story.ready, "timeout": 20000}]
    for kind, value in story.steps if steps is None else steps:
        if kind == "pause":
            out.append({"action": "pause", "ms": int(value)})
        elif kind == "type":
            # No selector: LittleDemo would first click the target, and xterm's
            # textarea is an off-screen 0x0 element it cannot target. The page
            # keeps that textarea focused, so plain text insertion lands there.
            out.append({"action": "type", "text": str(value), "charDelay": 0})
        elif kind == "key":
            out.append({"action": "key", "key": str(value)})
    config = {
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "waitFor": {"selector": story.ready},
        "steps": out,
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path
