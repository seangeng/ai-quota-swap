"""Play a sequence of menu bar drawings as one page, for ``menubar.gif``.

Each shot is a whole page drawn by ``menubar_page.page`` from a real
``build_menu`` (``run.py --menubar --auto-switch --frames``: the menu bar
after each of the engine's checks), embedded unchanged as an ``<iframe
srcdoc>``. This module adds no row and no title of its own: it only says
which drawing shows when, cross-fades from one to the next, and can show a
drawing magnified on the menu bar's right-hand end, so the title's figures
can be read while the menu is shut. Every magnified shot gets the same
scale, the largest that keeps each one's title and notification banner in
view.

The page opens on the first shot. A key press (any key) starts the play,
which follows a timeline computed here; the script in the page only applies
it. Every shot must have the same size (``--size``), so the menu bar stays
put while the drawings change under it. No external fonts, scripts or
images.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass

MAX_ZOOM = 2.4
ZOOM_MARGIN = 14  # CSS px of bar left of the widest title while magnified
PAD = 8  # backdrop around the drawing, as the dashboard recordings have


@dataclass(frozen=True)
class Shot:
    page: str  # a whole page from menubar_page.page()
    hold_ms: int  # on screen from the start of its fade-in to the next shot's
    fade_ms: int = 220  # its cross-fade in from the shot before
    zoom: bool = False  # magnified on the menu bar's right-hand end


def timeline(shots: list[Shot]) -> list[dict]:
    """``[{at, kind, ...}]``: ``show`` a shot (a cross-fade of ``ms``), then
    the ``end``. The first shot is on screen when the play starts."""
    events: list[dict] = []
    t = 0
    for i, shot in enumerate(shots):
        if i:
            events.append({"at": t, "kind": "show", "shot": i, "ms": shot.fade_ms})
        t += shot.hold_ms
    events.append({"at": t, "kind": "end"})
    return events


def duration_ms(shots: list[Shot]) -> int:
    return timeline(shots)[-1]["at"]


SCRIPT = """
(function () {
  var shots = document.querySelectorAll(".shot");
  var events = JSON.parse(document.getElementById("timeline").textContent);
  var W = %(width)d, MAX = %(max_zoom)s, MARGIN = %(margin)d;
  var top = 1;
  function show(i, ms) {
    var shot = shots[i];
    shot.style.transition = "opacity " + ms + "ms ease-in-out";
    shot.style.zIndex = ++top;
    shot.classList.add("on");
    setTimeout(function () {  // the one beneath is covered now
      for (var j = 0; j < shots.length; j++) {
        if (j !== i) { shots[j].style.transition = "none"; shots[j].classList.remove("on"); }
      }
    }, ms + 40);
  }
  function apply(e) {
    if (e.kind === "show") show(e.shot, e.ms);
    else if (e.kind === "end") document.body.classList.add("done");
  }
  function measure() {
    // Magnify about the bar's right-hand end, as far as keeps the widest
    // title, and any banner of a magnified shot, in view (the drawings are
    // same-origin, so their layout is here).
    var zoom = 1.8;
    try {
      var left = W;
      for (var i = 0; i < shots.length; i++) {
        var doc = shots[i].contentDocument;
        left = Math.min(left, doc.getElementById("status").getBoundingClientRect().left);
        var note = doc.getElementById("note");
        if (note && shots[i].classList.contains("zoomed")) {
          left = Math.min(left, note.getBoundingClientRect().left);
        }
      }
      zoom = Math.max(1, Math.min(MAX, (W - MARGIN) / (W - left)));
    } catch (err) {}
    document.getElementById("fit").style.setProperty("--zoom", zoom);
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
  var waiting = shots.length;
  function loaded() {
    if (--waiting) return;
    measure();
    document.body.classList.add("ready");
  }
  for (var k = 0; k < shots.length; k++) {
    var d = shots[k].contentDocument;
    if (d && d.readyState === "complete" && d.getElementById("stage")) loaded();
    else shots[k].addEventListener("load", loaded);
  }
})();
"""


def page(shots: list[Shot], *, width: int, height: int, scale: float = 1.0,
         theme: str = "dark", title: str = "qswap menu bar") -> str:
    """The page. ``width`` x ``height`` is every shot's size in CSS px (the
    ``--size`` they were drawn at); ``scale`` enlarges them for a recording."""
    if not shots:
        raise ValueError("no shots to play")
    frames = "".join(
        f'<iframe class="shot{" on" if i == 0 else ""}{" zoomed" if s.zoom else ""}" '
        f'title="menu bar, shot {i + 1}" width="{width}" height="{height}" scrolling="no" '
        f'srcdoc="{html.escape(s.page, quote=True)}"></iframe>'
        for i, s in enumerate(shots)
    )
    backdrop = ("radial-gradient(ellipse at 50% 40%, #26211d 0%, #0d0d0e 70%)" if theme == "dark"
                else "radial-gradient(ellipse at 50% 40%, #fffaf2 0%, #e9e3d8 75%)")
    script = SCRIPT % {"width": width, "max_zoom": f"{MAX_ZOOM:g}", "margin": ZOOM_MARGIN}
    events = json.dumps(timeline(shots), separators=(",", ":")).replace("</", "<\\/")
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
  #frame {{ padding: {PAD}px; }}
  #view {{ position: relative; overflow: hidden; width: {width * scale:g}px;
           height: {height * scale:g}px; border-radius: {12 * scale:g}px; }}
  #fit {{ position: absolute; left: 0; top: 0; width: {width}px; height: {height}px;
          transform: scale({scale:g}); transform-origin: 0 0; --zoom: 1; }}
  .shot {{ position: absolute; left: 0; top: 0; border: 0; opacity: 0; display: block;
           background: transparent; }}
  .shot.on {{ opacity: 1; }}
  .shot.zoomed {{ transform: scale(var(--zoom)); transform-origin: 100% 0; }}
  /* A recording is keyboard-only: the recorder's pointer and key overlays
     would sit over the drawing, so the page hides them. */
  #__ld_cursor, #__demo-cursor, #__demo-keys {{ display: none !important; }}
</style>
</head>
<body>
<div id="frame"><div id="view"><div id="fit">{frames}</div></div></div>
<script type="application/json" id="timeline">{events}</script>
<script>{script}</script>
</body>
</html>
"""
