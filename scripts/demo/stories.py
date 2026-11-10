"""Scripted dashboard recordings: which keys to press, and when.

One storyboard drives both recorders: LittleDemo (``to_littledemo`` writes
its ``steps.json``) and the local fallback (``cdp.record_frames`` plus
ffmpeg). Keys go into the TUI through xterm.js in the served page: printable
keys as typed text into its helper textarea, Enter/Escape as key presses.

Timing assumes the scenario's ``AutoRun.compression``: with 20x, each
one-minute engine tick takes three seconds of video.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

XTERM_INPUT = ".xterm-helper-textarea"
READY = "body.-first-byte"  # textual.js sets it once the app has drawn


@dataclass(frozen=True)
class Story:
    name: str
    scenario: str
    title: str
    prompt: str  # what the recording shows, for the LittleDemo upload
    steps: tuple[tuple[str, object], ...]  # ("pause", ms) | ("type", "g") | ("key", "Enter")
    view: str = "dashboard"


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
        ("pause", 2500),
        ("type", "g"),  # auto-switch view (opens in dry-run)
        ("pause", 2200),
        ("type", "l"),  # go live...
        ("pause", 1400),
        ("type", "y"),  # ...confirmed
        ("pause", 7500),
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
    steps=(
        ("pause", 2500),
        ("type", "g"),
        ("pause", 2200),
        ("type", "l"),
        ("pause", 1400),
        ("type", "y"),
        ("pause", 7000),
        ("key", "Escape"),  # back to the dashboard, window refilled
        ("pause", 3500),
    ),
)

STORIES = {s.name: s for s in (AUTOSWITCH, RESET)}


def duration_ms(story: Story) -> int:
    return sum(int(v) for k, v in story.steps if k == "pause") + 900 * sum(
        1 for k, _ in story.steps if k != "pause"
    )


def to_littledemo(story: Story, path: Path, *, viewport: tuple[int, int]) -> Path:
    """Write the story as a LittleDemo ``steps.json`` (``record --steps``)."""
    steps: list[dict] = [{"action": "wait", "selector": READY, "timeout": 20000}]
    for kind, value in story.steps:
        if kind == "pause":
            steps.append({"action": "pause", "ms": int(value)})
        elif kind == "type":
            # No selector: LittleDemo would first click the target, and xterm's
            # textarea is an off-screen 0x0 element it cannot target. The page
            # keeps that textarea focused, so plain text insertion lands there.
            steps.append({"action": "type", "text": str(value), "charDelay": 0})
        elif kind == "key":
            steps.append({"action": "key", "key": str(value)})
    config = {
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "waitFor": {"selector": READY},
        "steps": steps,
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path
