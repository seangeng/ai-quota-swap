"""Render captured CLI output (ANSI) as a self-contained terminal-window page.

The text is the tool's own output, colours included: ``printer.py`` emits its
palette (xterm 256-colour codes for dark, truecolor for light, bold and dim),
Rich turns the escape codes into styled spans, and the page wraps them in a
window with a title bar. Lines wrap at the terminal width the run used, the
way the terminal would show them (at word boundaries). No external fonts,
scripts or images: the page renders the same offline.
"""

from __future__ import annotations

import html
import io
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.terminal_theme import TerminalTheme
from rich.text import Text

FONT_SIZE = 15  # px, the same in every window, so text keeps one scale
PADDING_X = 28  # px on each side of the text

DARK = TerminalTheme(
    (23, 24, 28),
    (228, 228, 226),
    [
        (60, 62, 68), (232, 108, 108), (143, 196, 120), (226, 186, 106),
        (110, 166, 230), (196, 140, 220), (98, 190, 196), (212, 212, 208),
    ],
    [
        (96, 98, 106), (255, 128, 128), (168, 220, 140), (246, 206, 126),
        (130, 186, 250), (216, 160, 240), (118, 210, 216), (250, 250, 248),
    ],
)
LIGHT = TerminalTheme(
    (251, 250, 247),
    (36, 36, 34),
    [
        (36, 36, 34), (173, 49, 40), (46, 118, 52), (121, 89, 17),
        (36, 92, 170), (130, 60, 150), (24, 120, 128), (120, 118, 112),
    ],
    [
        (80, 80, 76), (200, 70, 60), (60, 140, 70), (150, 110, 30),
        (60, 110, 190), (150, 80, 170), (40, 140, 150), (200, 198, 192),
    ],
)

_CHROME = {
    "dark": {
        "bg": "#17181c", "fg": "#e4e4e2", "chrome": "#222327", "chrome_fg": "#9a9a98",
        "border": "#33343a", "prompt": "#8a8a88",
    },
    "light": {
        "bg": "#fbfaf7", "fg": "#242422", "chrome": "#ecebe7", "chrome_fg": "#6d6c68",
        "border": "#d9d7d1", "prompt": "#8a8984",
    },
}


@dataclass
class Block:
    """One command and what it printed."""

    command: str
    output: str


def ansi_to_html(text: str, *, theme: str, columns: int) -> str:
    console = Console(
        record=True,
        width=columns,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
    )
    body = Text.from_ansi(text.rstrip("\n"))
    console.print(body, overflow="fold", crop=False)
    return console.export_html(
        theme=DARK if theme == "dark" else LIGHT,
        inline_styles=True,
        code_format="{code}",
    ).rstrip("\n")


def page(blocks: list[Block], *, title: str, theme: str, columns: int) -> str:
    c = _CHROME[theme]
    parts = []
    for i, b in enumerate(blocks):
        if i:
            parts.append("\n")
        parts.append(
            f'<span class="prompt">$</span> <span class="cmd">{html.escape(b.command)}</span>\n'
        )
        out = ansi_to_html(b.output, theme=theme, columns=columns)
        if out:
            parts.append(out + "\n")
    screen = "".join(parts).rstrip("\n")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<meta name="color-scheme" content="{theme}">
<title>{html.escape(title)}</title>
<style>
  html, body {{ margin: 0; padding: 0; background: transparent; }}
  body {{ display: flex; justify-content: center; }}
  .window {{
    width: fit-content; box-sizing: border-box; overflow: hidden;
    background: {c["bg"]}; border: 1px solid {c["border"]}; border-radius: 12px;
  }}
  .titlebar {{
    position: relative; height: 40px; display: flex; align-items: center;
    background: {c["chrome"]}; border-bottom: 1px solid {c["border"]};
  }}
  .dots {{ display: flex; gap: 8px; padding-left: 16px; }}
  .dots i {{ display: block; width: 12px; height: 12px; border-radius: 50%; }}
  .dots i:nth-child(1) {{ background: #ff5f57; }}
  .dots i:nth-child(2) {{ background: #febc2e; }}
  .dots i:nth-child(3) {{ background: #28c840; }}
  .title {{
    position: absolute; left: 0; right: 0; text-align: center; pointer-events: none;
    font: 500 13px -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    color: {c["chrome_fg"]};
  }}
  .screen {{
    margin: 0; padding: 20px {PADDING_X}px 24px; color: {c["fg"]};
    width: {columns}ch;
    font-family: "SF Mono", SFMono-Regular, ui-monospace, Menlo, Monaco, Consolas,
      "DejaVu Sans Mono", "Liberation Mono", monospace;
    font-size: {FONT_SIZE}px; line-height: 1.5; white-space: pre;
    font-variant-ligatures: none; -webkit-font-smoothing: antialiased;
    tab-size: 8;
  }}
  .prompt {{ color: {c["prompt"]}; }}
  .cmd {{ font-weight: 600; }}
</style>
</head>
<body>
<div class="window" id="window">
  <div class="titlebar"><div class="dots"><i></i><i></i><i></i></div>
    <div class="title">{html.escape(title)}</div></div>
<pre class="screen">{screen}</pre>
</div>
</body>
</html>
"""


def write_page(path: Path, blocks: list[Block], *, title: str, theme: str, columns: int) -> None:
    path.write_text(page(blocks, title=title, theme=theme, columns=columns), encoding="utf-8")
