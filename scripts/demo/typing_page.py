"""A terminal page that types each command and then shows what it printed.

For recordings (``quickstart.gif``): ``run.py <scenario> --typing --html FILE
-- cmd ++ cmd ...`` runs the real commands one after another on one state
and hands their output here. The output is the tool's own, rendered by
``render.ansi_to_html`` in the CLI shots' window; only the typing is an
animation, played by a few lines of script in the page.

The page opens on its last screen (every command typed and answered), which
makes a good first frame for a GIF. A key press (any key) starts the play:
the screen clears to a prompt, each command is typed a character at a time,
its output appears whole, as a command's would, and the screen scrolls like a
terminal of ``rows`` lines. It ends on the last screen again, so a looping GIF
has no seam. The timeline is computed here, so a page is the same for the
same output; the script only follows it. No external fonts, scripts or
images.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass

import render

# Milliseconds. Typing varies a little per character, the same way every time.
START_HOLD = 1200  # the last screen, before it clears
CLEARED = 500  # an empty prompt before the first key
KEY_BASE, KEY_SPREAD = 34, 30
BEFORE_ENTER = 320
AFTER_ENTER = 140  # the command's start-up, before its output appears
HOLD_BASE, HOLD_PER_LINE, HOLD_MIN, HOLD_MAX = 900, 150, 1400, 3200
END_HOLD = 2600

LINE_HEIGHT = 1.5  # em, as render.py's screen


@dataclass(frozen=True)
class Event:
    at: int  # ms after the start
    kind: str  # "clear" | "type" | "enter" | "output" | "end"
    block: int = -1
    chars: int = 0


def key_delay(command: str, i: int) -> int:
    """The pause before character ``i`` of ``command`` is typed."""
    ch = command[i]
    delay = KEY_BASE + (i * 37 + ord(ch) * 11) % KEY_SPREAD
    if ch == " ":
        delay += 40
    return delay


def hold(output: str) -> int:
    """How long one command's output stays before the next is typed."""
    lines = output.rstrip("\n").count("\n") + 1 if output.strip() else 0
    return max(HOLD_MIN, min(HOLD_MAX, HOLD_BASE + HOLD_PER_LINE * lines))


def timeline(blocks: list[render.Block]) -> list[Event]:
    events = [Event(START_HOLD, "clear")]
    t = START_HOLD + CLEARED
    for b, block in enumerate(blocks):
        for i in range(len(block.command)):
            t += key_delay(block.command, i)
            events.append(Event(t, "type", b, i + 1))
        t += BEFORE_ENTER
        events.append(Event(t, "enter", b))
        t += AFTER_ENTER
        events.append(Event(t, "output", b))
        t += hold(block.output) if b + 1 < len(blocks) else END_HOLD
    events.append(Event(t, "end"))
    return events


def duration_ms(blocks: list[render.Block]) -> int:
    return timeline(blocks)[-1].at


SCRIPT = """
(function () {
  var screen = document.getElementById("screen");
  var events = JSON.parse(document.getElementById("timeline").textContent);
  var commands = JSON.parse(document.getElementById("commands").textContent);
  function el(id) { return document.getElementById(id); }
  function scroll() { screen.scrollTop = screen.scrollHeight; }
  function only(id) {  // the cursor sits on one prompt at a time
    var c = document.querySelectorAll(".cur");
    for (var i = 0; i < c.length; i++) c[i].hidden = c[i].id !== id;
  }
  function clear() {
    var parts = screen.querySelectorAll(".part");
    for (var i = 0; i < parts.length; i++) parts[i].hidden = true;
    for (var b = 0; b < commands.length; b++) el("cmd" + b).textContent = "";
    el("line0").hidden = false;
    only("cur0");
    scroll();
  }
  function apply(e) {
    if (e.kind === "clear") clear();
    else if (e.kind === "type") el("cmd" + e.block).textContent = commands[e.block].slice(0, e.chars);
    else if (e.kind === "enter") only("");
    else if (e.kind === "output") {
      el("out" + e.block).hidden = false;
      var next = e.block + 1 < commands.length ? e.block + 1 : "end";
      if (el("gap" + next)) el("gap" + next).hidden = false;
      el("line" + next).hidden = false;
      only("cur" + next);
      scroll();
    } else if (e.kind === "end") document.body.classList.add("done");
  }
  var started = false;
  function start() {
    if (started) return;
    started = true;
    document.body.classList.add("playing");
    events.forEach(function (e) { setTimeout(function () { apply(e); }, e.at); });
  }
  window.__play = start;
  document.addEventListener("keydown", start);
  scroll();
  document.body.classList.add("ready");
})();
"""


def page(blocks: list[render.Block], *, title: str, theme: str, columns: int, rows: int,
         zoom: float = 1.0, stage: bool = True) -> str:
    """The page: ``blocks`` are the commands and what they printed (ANSI),
    ``rows`` the screen's height in lines. ``zoom`` scales the window for a
    recording; ``stage`` puts it on a dark backdrop, as the dashboard's
    recording page does."""
    parts = []
    for b, block in enumerate(blocks):
        if b:
            parts.append(f'<div class="part" id="gap{b}"> </div>')
        parts.append(
            f'<div class="part" id="line{b}"><span class="prompt">$</span> '
            f'<span class="cmd" id="cmd{b}">{html.escape(block.command)}</span>'
            f'<span class="cur" id="cur{b}" hidden></span></div>'
        )
        out = render.ansi_to_html(block.output, theme=theme, columns=columns)
        parts.append(f'<div class="part out" id="out{b}">{out}</div>')
    parts.append('<div class="part" id="gapend"> </div>')
    parts.append('<div class="part" id="lineend"><span class="prompt">$</span> '
                 '<span class="cur" id="curend"></span></div>')
    c = render._CHROME[theme]
    events = [e.__dict__ for e in timeline(blocks)]
    backdrop = (
        "radial-gradient(ellipse at 50% 40%, #26211d 0%, #0d0d0e 70%)" if theme == "dark"
        else "radial-gradient(ellipse at 50% 40%, #fffaf2 0%, #e9e3d8 75%)"
    ) if stage else "transparent"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<meta name="color-scheme" content="{theme}">
<title>{html.escape(title)}</title>
<style>
  html, body {{ margin: 0; padding: 0; }}
  body {{ display: flex; justify-content: center; align-items: center; min-height: 100vh;
          background: {backdrop}; }}
  #frame {{ zoom: {zoom:g}; padding: 8px; }}
{render.window_css(theme, columns)}
  .window {{ box-shadow: 0 24px 60px rgba(0, 0, 0, 0.45), 0 2px 8px rgba(0, 0, 0, 0.3); }}
  /* The lines scroll inside the screen's padding, as a terminal's do. */
  .scroll {{ height: {rows * LINE_HEIGHT:g}em; overflow: hidden; }}
  .scroll > div {{ min-height: {LINE_HEIGHT:g}em; }}
  .cur {{ display: inline-block; width: 0.6em; height: 1.15em; vertical-align: -0.2em;
          margin-left: 1px; background: {c["fg"]}; opacity: 0.85; }}
  .cur[hidden], .part[hidden] {{ display: none; }}
  /* A recording is keyboard-only: the recorder's pointer and key overlays
     would sit over the drawing, so the page hides them. */
  #__ld_cursor, #__demo-cursor, #__demo-keys {{ display: none !important; }}
</style>
</head>
<body>
<div id="frame">
{render.window_html(title, f'<div class="screen"><div class="scroll" id="screen">{"".join(parts)}</div></div>')}
</div>
<script type="application/json" id="commands">{_json([b.command for b in blocks])}</script>
<script type="application/json" id="timeline">{_json(events)}</script>
<script>{SCRIPT}</script>
</body>
</html>
"""


def _json(value) -> str:
    # Safe inside <script>: no "</" can close the element early.
    return json.dumps(value, separators=(",", ":")).replace("</", "<\\/")
