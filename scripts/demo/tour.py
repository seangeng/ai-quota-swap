"""Screenshot every dashboard screen and modal, at exact terminal sizes.

    uv run python scripts/demo/tour.py OUT_DIR                     # every stop, dark and light, 100x30 and 80x24
    uv run python scripts/demo/tour.py OUT_DIR --only main,watch --themes dark --sizes 100x30

A review aid, separate from the README shots (``capture.py``): each stop is a
scenario, a key sequence from the dashboard's first frame, and a name. For
each scenario and theme one guarded server runs (``run.py --serve``); every
stop opens a fresh page, which textual-serve answers with a fresh, fully
guarded app process, so no stop sees another's state. The terminal is sized
in cells, not pixels: the page reports the size xterm.js fits, and the window
is resized until it is exactly the requested columns and rows.

Files are named ``<stop>--<theme>--<cols>x<rows>.png``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import capture  # noqa: E402
import cdp  # noqa: E402

FONT = 16
READY = capture.READY
# Recorded by the page: the size xterm.js last sent the app ("resize" frames).
SIZE_HOOK = """
(() => {
  const Real = window.WebSocket;
  window.__qswapCells = null;
  window.WebSocket = function (url, protocols) {
    const ws = protocols === undefined ? new Real(url) : new Real(url, protocols);
    const m = /[?&]width=(\\d+)&height=(\\d+)/.exec(url);
    if (m) window.__qswapCells = {cols: +m[1], rows: +m[2]};
    const send = ws.send.bind(ws);
    ws.send = (data) => {
      try {
        const msg = JSON.parse(data);
        if (msg[0] === "resize") window.__qswapCells = {cols: msg[1].width, rows: msg[1].height};
      } catch (e) {}
      return send(data);
    };
    return ws;
  };
  window.WebSocket.prototype = Real.prototype;
  Object.assign(window.WebSocket, Real);
})();
"""

# Root menu order (tui/dashboard.py ROOT_MENU), for a fleet with nothing to add first.
MENU = ("switch", "watch", "auto", "usage", "add", "disable", "remove", "theme", "quit")


def menu(entry: str) -> tuple:
    """Keys that open a root-menu entry from the dashboard's first frame."""
    return (("key", "ArrowDown"),) * MENU.index(entry) + (("key", "Enter"), ("wait", 0.8))


@dataclass(frozen=True)
class Stop:
    name: str
    scenario: str
    steps: tuple  # ("key", "Enter") | ("type", "g") | ("wait", seconds)
    caption: str
    settle: float = 2.5  # seconds after the first frame before the first key
    sizes: tuple = ()  # only at these sizes (COLSxROWS); () means every size asked for


STOPS = (
    Stop("main", "fleet", (), "The dashboard (bare qswap / qswap tui): both providers' accounts and the menu"),
    Stop("cost-line", "usage", (), "The dashboard with session history: the cost line under the title"),
    Stop("watch", "fleet", (("type", "w"), ("wait", 1.5)), "Watch page (w, or qswap watch): every account, read-only"),
    Stop("watch-select", "fleet", (("type", "w"), ("wait", 1.0), ("type", "s"), ("wait", 0.8)),
         "Watch page with selection armed (s)"),
    Stop("auto-dryrun", "fleet", (("type", "g"), ("wait", 3.0)),
         "Auto-switch view (g), dry run: Claude Code's panel, Tab for Codex's"),
    Stop("auto-codex", "fleet", (("type", "g"), ("wait", 2.5), ("key", "Tab"), ("wait", 0.8)),
         "Auto-switch view: Codex's panel (Tab)"),
    Stop("auto-wide", "fleet", (("type", "g"), ("wait", 3.0)),
         "Auto-switch view on a wide terminal: both providers side by side", sizes=((150, 36),)),
    Stop("auto-threshold", "fleet", (("type", "g"), ("wait", 2.0), ("type", "t"), ("wait", 0.8)),
         "Auto-switch view, threshold adjust (t)"),
    Stop("auto-live-confirm", "fleet", (("type", "g"), ("wait", 2.0), ("type", "l"), ("wait", 0.8)),
         "Auto-switch view: confirm going live (l)"),
    Stop("auto-live", "fleet", (("type", "g"), ("wait", 2.0), ("type", "l"), ("wait", 0.8),
                                ("type", "y"), ("wait", 4.0)), "Auto-switch view, live"),
    Stop("auto-live-switched", "autoswitch", (("type", "g"), ("wait", 2.0), ("type", "l"), ("wait", 0.8),
                                              ("type", "y"), ("wait", 9.0)),
         "Auto-switch view, live, after the engine switched (autoswitch scenario)"),
    Stop("usage", "usage", (("type", "u"), ("wait", 3.0)), "Usage report (u): the last 7 days by day"),
    Stop("usage-by-model", "usage", (("type", "u"), ("wait", 2.5), ("key", "Tab"), ("wait", 1.5)),
         "Usage report regrouped by model (Tab)"),
    Stop("usage-30-days", "usage", (("type", "u"), ("wait", 2.5), ("key", "Tab"), ("key", "Tab"),
                                    ("type", "d"), ("wait", 2.5)),
         "Usage report by account over 30 days (Tab, d)"),
    Stop("switch-picker", "fleet", (("type", "s"), ("wait", 1.2)), "Switch picker (s)"),
    Stop("switch-done", "fleet", (("type", "s"), ("wait", 1.2), ("key", "ArrowDown"), ("key", "Enter"),
                                  ("wait", 1.5)), "Back on the dashboard after switching to account 2"),
    Stop("switch-anyway", "trouble", (("type", "s"), ("wait", 1.2), ("key", "ArrowDown"), ("key", "Enter"),
                                      ("wait", 1.5)),
         "Switching to an account that needs re-login: refused, with a choice to switch anyway"),
    Stop("add-menu", "fleet", menu("add"), "Add account submenu: what each provider can add"),
    Stop("add-login-confirm", "fleet", menu("add") + (("key", "Enter"), ("wait", 0.8)),
         "Add from the current Claude Code login: confirmation"),
    Stop("add-login-output", "empty-claude", (("key", "Enter"), ("wait", 0.8), ("type", "y"), ("wait", 2.5)),
         "First run: Add <the live login>, confirmed"),
    Stop("add-token-form", "fleet", menu("add") + (("key", "ArrowDown"), ("key", "Enter"), ("wait", 0.8)),
         "Add from a setup-token / API key: the form"),
    Stop("codex-login", "fleet", menu("add") + (("key", "ArrowDown"),) * 3 + (("key", "Enter"), ("wait", 0.8)),
         "Log in another Codex account: the command, where the dashboard cannot step aside"),
    Stop("disable-menu", "fleet", menu("disable"), "Disable / enable submenu"),
    Stop("disable-done", "fleet", menu("disable") + (("key", "ArrowDown"), ("key", "Enter"), ("wait", 1.5)),
         "After disabling account 2"),
    Stop("remove-menu", "fleet", menu("remove"), "Remove account submenu"),
    Stop("remove-confirm", "fleet", menu("remove") + (("key", "ArrowDown"), ("key", "Enter"), ("wait", 0.8)),
         "Remove account: confirmation"),
    Stop("theme-menu", "fleet", menu("theme"), "Theme submenu"),
    Stop("first-run-no-login", "empty", (), "The dashboard on a machine with no Claude Code login"),
    Stop("first-run-login", "empty-claude", (), "The dashboard with a live login that is not managed yet"),
    Stop("first-run-both", "empty-claude-codex", (),
         "The dashboard with live Claude Code and Codex logins, neither managed"),
    Stop("trouble-main", "trouble", (), "The dashboard with accounts in failure states"),
    Stop("trouble-watch", "trouble", (("type", "w"), ("wait", 1.5)), "Watch page with accounts in failure states"),
)

def parse_size(text: str) -> tuple[int, int]:
    cols, _, rows = text.lower().partition("x")
    return int(cols), int(rows)


async def fit_cells(new_page, server, theme: str, cols: int, rows: int,
                    font: int = FONT) -> tuple[int, int]:
    """Pixel width and height of the terminal area that xterm.js fits to
    exactly ``cols`` x ``rows`` at ``font`` px."""
    w, h = round(0.6 * font * cols) + 40, round(1.25 * font * rows) + 40
    # Each fit floors pixels / cell size, so each answer bounds the cell size;
    # the next try aims for the middle of the target cell.
    cw_lo, cw_hi, ch_lo, ch_hi = 0.0, float("inf"), 0.0, float("inf")
    got: dict = {}
    for _ in range(10):
        page = await new_page()
        await page.setup(w + 200, h + 200)
        await page.send("Page.addScriptToEvaluateOnNewDocument", {"source": SIZE_HOOK})
        await page.goto(server.url(f"fontsize={font}&w={w}&h={h}&theme={theme}"))
        await page.wait_for(READY)
        await page.wait_for("window.__qswapCells !== null")
        got = json.loads(await page.eval("JSON.stringify(window.__qswapCells)"))
        await page.close()
        if got == {"cols": cols, "rows": rows}:
            return w, h
        c, r = max(1, got["cols"]), max(1, got["rows"])
        cw_lo, cw_hi = max(cw_lo, w / (c + 1)), min(cw_hi, w / c)
        ch_lo, ch_hi = max(ch_lo, h / (r + 1)), min(ch_hi, h / r)
        w = int((cols + 0.5) * (cw_lo + cw_hi) / 2)
        h = int((rows + 0.5) * (ch_lo + ch_hi) / 2)
    raise RuntimeError(f"could not fit {cols}x{rows} (last {got})")


async def play(page, steps) -> None:
    for kind, value in steps:
        if kind == "wait":
            await asyncio.sleep(float(value))
        elif kind == "type":
            await page.type_text(str(value))
            await asyncio.sleep(0.25)
        elif kind == "key":
            await page.press(str(value))
            await asyncio.sleep(0.25)


def target_path(out: Path, stop: Stop, theme: str, cols: int, rows: int) -> Path:
    return out / f"{stop.name}--{theme}--{cols}x{rows}.png"


async def shoot_scenario(scenario: str, theme: str, sizes, stops, out: Path, log,
                         skip_existing: bool = False) -> list[Path]:
    made = []
    with capture.Server(scenario, theme=theme) as server:
        async with cdp.chrome(2) as new_page:
            wanted = list(dict.fromkeys([*sizes, *(z for s in stops for z in s.sizes)]))
            for cols, rows in wanted:
                todo = [s for s in stops
                        if ((cols, rows) in s.sizes or (not s.sizes and (cols, rows) in sizes))
                        and not (skip_existing and target_path(out, s, theme, cols, rows).exists())]
                if not todo:
                    continue
                w, h = await fit_cells(new_page, server, theme, cols, rows)
                for stop in todo:
                    page = await new_page()
                    await page.setup(w + 120, h + 160)
                    await page.goto(server.url(f"fontsize={FONT}&w={w}&h={h}&theme={theme}"))
                    await page.wait_for(READY)
                    await asyncio.sleep(stop.settle)
                    await play(page, stop.steps)
                    await asyncio.sleep(0.6)
                    target = target_path(out, stop, theme, cols, rows)
                    await page.screenshot(target, "#window")
                    await page.close()
                    log(f"png    {target.name}")
                    made.append(target)
    return made


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tour.py", description=__doc__.split("\n\n")[0])
    p.add_argument("out", type=Path, help="directory for the PNGs")
    p.add_argument("--only", help="comma-separated stop names")
    p.add_argument("--themes", default="dark,light")
    p.add_argument("--sizes", default="100x30,80x24", help="comma-separated COLSxROWS")
    p.add_argument("--list", action="store_true", help="print the stops and exit")
    p.add_argument("--skip-existing", action="store_true", help="keep PNGs already in OUT_DIR")
    args = p.parse_args(argv)
    if args.list:
        for s in STOPS:
            print(f"{s.name:22} {s.scenario:14} {s.caption}")
        return 0
    only = set(args.only.split(",")) if args.only else None
    stops = [s for s in STOPS if only is None or s.name in only]
    sizes = [parse_size(s) for s in args.sizes.split(",")]
    args.out.mkdir(parents=True, exist_ok=True)
    for theme in args.themes.split(","):
        for scenario in dict.fromkeys(s.scenario for s in stops):
            mine = [s for s in stops if s.scenario == scenario]
            asyncio.run(shoot_scenario(scenario, theme, sizes, mine, args.out, capture.log,
                                       skip_existing=args.skip_existing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
